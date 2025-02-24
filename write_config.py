#!/usr/bin/env python3
"""
Generate a hound config.json file
Copyright (C) 2017-2018 Kunal Mehta <legoktm@debian.org>

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""

import argparse
import base64
from configparser import ConfigParser
import functools
import json
import os
import requests
import subprocess
from typing import List
import yaml

# 90 minutes
POLL = 90 * 60 * 1000
DATA = '/srv/hound'


@functools.lru_cache()
def get_extdist_repos() -> dict:
    r = requests.get(
        'https://www.mediawiki.org/w/api.php',
        params={
            "action": "query",
            "format": "json",
            'formatversion': "2",
            "list": "extdistrepos"
        }
    )
    r.raise_for_status()

    return r.json()


@functools.lru_cache()
def parse_gitmodules(url):
    r = requests.get(url)
    r.raise_for_status()
    config = ConfigParser()
    config.read_string(r.text)
    repos = []
    for section in config.sections():
        # TODO: Use a proper URL parser instead of string manipulation
        url = config[section]['url']
        if url.endswith('.git'):
            url = url[:-4]
        if 'github.com' in url:
            name = url.replace('git@github.com:', '').replace('https://github.com/', '')
            repos.append((name, gh_repo(name)))
        elif 'bitbucket.org' in url:
            name = url.replace('https://bitbucket.org/', '')
            repos.append((name, bitbucket_repo(name)))
        elif 'gitlab.com' in url:
            name = url.replace('https://gitlab.com/', '')
            repos.append((name, gitlab_repo(name)))
        elif 'invent.kde.org' in url:
            name = url.replace('https://invent.kde.org/', '')
            repos.append((name, gh_repo(name, host='invent.kde.org')))
        elif 'phabricator.nichework.com' in url:
            # FIXME: implement
            continue
        else:
            raise RuntimeError(f'Unsure how to handle URL: {url}')

    return repos


def _get_gerrit_file(gerrit_name: str, path: str) -> str:
    url = f'https://gerrit.wikimedia.org/g/{gerrit_name}/+/master/{path}?format=TEXT'
    print('Fetching ' + url)
    r = requests.get(url)
    return base64.b64decode(r.text).decode()


@functools.lru_cache()
def _settings_yaml() -> dict:
    return yaml.safe_load(_get_gerrit_file('mediawiki/tools/release',
                                           'make-release/settings.yaml'))


def gerrit_prefix_list(prefix: str) -> dict:
    """Generator based on Gerrit prefix search"""
    req = requests.get('https://gerrit.wikimedia.org/r/projects/', params={
        'p': prefix,
    })
    req.raise_for_status()
    data = json.loads(req.text[4:])
    repos = {}
    for repo in data:
        info = data[repo]
        if info['state'] != 'ACTIVE':
            continue
        repos[repo] = repo_info(repo)

    return repos


def bundled_repos() -> List[str]:
    return [name for name in _settings_yaml()['bundles']['base']]


def wikimedia_deployed_repos() -> List[str]:
    return [name for name in _settings_yaml()['bundles']['wmf_core']]


def phab_repo(name: str) -> dict:
    return {
        'url': f'https://phabricator.wikimedia.org/source/{name}',
        'url-pattern': {
            'base-url': 'https://phabricator.wikimedia.org/source/'
                        '%s/browse/master/{path};{rev}{anchor}' % name,
            'anchor': '${line}'
        },
        'ms-between-poll': POLL,
    }


def repo_info(gerrit_name: str) -> dict:
    return {
        'url': f'https://gerrit-replica.wikimedia.org/r/{gerrit_name}.git',
        'url-pattern': {
            'base-url': 'https://gerrit.wikimedia.org/g/' +
                        '%s/+/{rev}/{path}{anchor}' % gerrit_name,
            'anchor': '#{line}'
        },
        'ms-between-poll': POLL,
    }


def bitbucket_repo(bb_name: str) -> dict:
    return {
        'url': f'https://bitbucket.org/{bb_name}.git',
        'url-pattern': {
            'base-url': 'https://bitbucket.org/%s/src/{rev}/{path}' % bb_name,
            # The anchor syntax used by bitbucket is too complicated for hound to
            # be able to generate. It's `#basename({path})-{line}`.
            'anchor': ''
        },
        'ms-between-poll': POLL,
    }


def gitlab_repo(gl_name: str) -> dict:
    # Lazy/avoid duplication
    return gh_repo(gl_name, host='gitlab.com')


def gh_repo(gh_name: str, host: str = 'github.com') -> dict:
    return {
        'url': f'https://{host}/{gh_name}',
        'ms-between-poll': POLL,
    }


def make_conf(name, args, core=False, exts=False, skins=False, ooui=False,
              operations=False, armchairgm=False, twn=False, milkshake=False,
              bundled=False, vendor=False, wikimedia=False, pywikibot=False,
              services=False, libs=False, analytics=False, puppet=False,
              shouthow=False, schemas=False, wmcs=False):
    conf = {
        'max-concurrent-indexers': 2,
        'dbpath': 'data',
        'vcs-config': {
            'git': {
                'detect-ref': True
            },
        },
        'repos': {}
    }

    if core:
        conf['repos']['MediaWiki core'] = repo_info('mediawiki/core')

    if pywikibot:
        conf['repos']['Pywikibot'] = repo_info('pywikibot/core')

    if ooui:
        conf['repos']['OOUI'] = repo_info('oojs/ui')

    data = get_extdist_repos()
    if exts:
        # Sanity check (T223771)
        if not data['query']['extdistrepos']['extensions']:
            raise RuntimeError('Why are there no Gerrit extensions?')
        for ext in data['query']['extdistrepos']['extensions']:
            conf['repos']['Extension:%s' % ext] = repo_info(
                'mediawiki/extensions/%s' % ext
            )
        conf['repos']['VisualEditor core'] = repo_info(
            'VisualEditor/VisualEditor'
        )
        for repo_name, info in parse_gitmodules(
                "https://raw.githubusercontent.com/MWStake/nonwmf-extensions/master/.gitmodules"
        ):
            conf['repos'][repo_name] = info

    if skins:
        for skin in data['query']['extdistrepos']['skins']:
            conf['repos']['Skin:%s' % skin] = repo_info(
                'mediawiki/skins/%s' % skin
            )

        for repo_name, info in parse_gitmodules(
                "https://raw.githubusercontent.com/MWStake/nonwmf-skins/master/.gitmodules"
        ):
            conf['repos'][repo_name] = info

    if puppet:
        conf['repos']['Wikimedia Puppet'] = repo_info('operations/puppet')
        conf['repos']['labs/private'] = repo_info('labs/private')

    if puppet or wmcs:
        conf['repos']['cloud/instance-puppet'] = repo_info('cloud/instance-puppet')
        # instance-puppet for the codfw1dev testing deployment
        conf['repos']['cloud/instance-puppet-dev'] = repo_info('cloud/instance-puppet-dev')

    if operations:
        conf['repos']['Wikimedia DNS'] = repo_info(
            'operations/dns'
        )
        # Special Netbox repo
        conf['repos']['netbox DNS'] = phab_repo('netbox-exported-dns')
        conf['repos']['Wikimedia MediaWiki config'] = repo_info(
            'operations/mediawiki-config'
        )
        conf['repos']['scap'] = repo_info(
            'mediawiki/tools/scap'
        )
        # CI config T217716
        conf['repos']['Wikimedia continuous integration config'] = repo_info(
            'integration/config'
        )
        conf['repos']['Blubber'] = repo_info('blubber')
        conf['repos']['pipelinelib'] = repo_info('integration/pipelinelib')

        # TODO: Move this to a dedicated section like "development tools"
        conf['repos']['MediaWiki Vagrant'] = repo_info(
            'mediawiki/vagrant'
        )
        conf['repos']['operations/cookbooks'] = repo_info('operations/cookbooks')
        conf['repos']['operations/deployment-charts'] = repo_info(
            'operations/deployment-charts'
        )
        conf['repos']['operations/software'] = repo_info('operations/software')
        conf['repos']['operations/software/conftool'] = repo_info(
            'operations/software/conftool'
        )
        conf['repos']['operations/software/spicerack'] = repo_info(
            'operations/software/spicerack'
        )
        conf['repos']['operations/software/purged'] = repo_info(
            'operations/software/purged'
        )

        conf['repos']['performance/arc-lamp'] = repo_info('performance/arc-lamp')
        conf['repos']['performance/asoranking'] = repo_info('performance/asoranking')
        conf['repos']['performance/bttostatsv'] = repo_info('performance/bttostatsv')
        conf['repos']['performance/coal'] = repo_info('performance/coal')
        conf['repos']['performance/docroot'] = repo_info('performance/docroot')
        conf['repos']['performance/fresnel'] = repo_info('performance/fresnel')
        conf['repos']['performance/mobile-synthetic-monitoring-tests'] = repo_info(
            'performance/mobile-synthetic-monitoring-tests'
        )
        conf['repos']['performance/navtiming'] = repo_info('performance/navtiming')
        conf['repos']['performance/synthetic-monitoring-tests'] = repo_info(
            'performance/synthetic-monitoring-tests'
        )
        conf['repos']['performance/WikimediaDebug'] = repo_info('performance/WikimediaDebug')

    if armchairgm:
        conf['repos']['ArmchairGM'] = gh_repo('mary-kate/ArmchairGM')

    if twn:
        conf['repos']['translatewiki.net'] = repo_info('translatewiki')

    if milkshake:
        ms_repos = ['jquery.uls', 'jquery.ime', 'jquery.webfonts', 'jquery.i18n',
                    'language-data']
        for ms_repo in ms_repos:
            conf['repos'][ms_repo] = gh_repo('wikimedia/' + ms_repo)

    if bundled:
        for repo_name in bundled_repos():
            conf['repos'][repo_name] = repo_info(repo_name)

    if wikimedia:
        for repo_name in wikimedia_deployed_repos():
            conf['repos'][repo_name] = repo_info(repo_name)
        # Also mw-config (T214341)
        conf['repos']['Wikimedia MediaWiki config'] = repo_info(
            'operations/mediawiki-config'
        )
        conf['repos']['WikimediaDebug'] = repo_info('performance/WikimediaDebug')

    if vendor:
        conf['repos']['mediawiki/vendor'] = repo_info('mediawiki/vendor')

    if services:
        conf['repos'].update(gerrit_prefix_list('mediawiki/services/'))
        conf['repos']['mwaddlink'] = repo_info('research/mwaddlink')
        conf['repos']['Wikidata Query GUI'] = repo_info('wikidata/query/gui')
        conf['repos']['Wikidata Query RDF'] = repo_info('wikidata/query/rdf')

    if libs:
        conf['repos'].update(gerrit_prefix_list('mediawiki/libs/'))
        conf['repos']['AhoCorasick'] = repo_info('AhoCorasick')
        conf['repos']['cdb'] = repo_info('cdb')
        conf['repos']['CLDRPluralRuleParser'] = repo_info('CLDRPluralRuleParser')
        conf['repos']['HtmlFormatter'] = repo_info('HtmlFormatter')
        conf['repos']['IPSet'] = repo_info('IPSet')
        conf['repos']['RelPath'] = repo_info('RelPath')
        conf['repos']['RunningStat'] = repo_info('RunningStat')
        conf['repos']['WrappedString'] = repo_info('WrappedString')
        conf['repos']['MediaWiki CodeSniffer'] = repo_info(
            'mediawiki/tools/codesniffer'
        )
        conf['repos']['MediaWiki Phan'] = repo_info('mediawiki/tools/phan')
        conf['repos']['SecurityCheckPlugin'] = repo_info(
            'mediawiki/tools/phan/SecurityCheckPlugin'
        )
        conf['repos']['Purtle'] = repo_info('purtle')

        conf['repos']['wvui'] = repo_info('wvui')
        conf['repos']['codex'] = repo_info('design/codex')

        # Wikibase libraries
        conf['repos']['WikibaseDataModel'] = gh_repo('wmde/WikibaseDataModel')
        conf['repos']['WikibaseDataModelSerialization'] = \
            gh_repo('wmde/WikibaseDataModelSerialization')
        conf['repos']['WikibaseDataModelServices'] = gh_repo('wmde/WikibaseDataModelServices')
        conf['repos']['WikibaseInternalSerialization'] = \
            gh_repo('wmde/WikibaseInternalSerialization')
        conf['repos']['wikibase-termbox'] = repo_info('wikibase/termbox')
        conf['repos']['wikibase-vuejs-components'] = repo_info('wikibase/vuejs-components')
        conf['repos']['WikibaseDataValuesValueView'] = repo_info('data-values/value-view')
        conf['repos']['WikibaseJavascriptAPI'] = repo_info('wikibase/javascript-api')
        conf['repos']['WikibaseDataValuesJavaScript'] = gh_repo('wmde/DataValuesJavaScript')
        conf['repos']['WikibaseSerializationJavaScript'] = \
            gh_repo('wmde/WikibaseSerializationJavaScript')
        conf['repos']['WikibaseDataModelJavaScript'] = gh_repo('wmde/WikibaseDataModelJavaScript')

    if analytics:
        conf['repos'].update(gerrit_prefix_list('analytics/'))
    if schemas:
        # schemas/event/ requested in T275705
        conf['repos'].update(gerrit_prefix_list('schemas/event/'))

    if shouthow:
        conf['repos']['ShoutHow'] = gh_repo('ashley/ShoutHow', host='git.legoktm.com')

    if wmcs:
        # toolforge infra
        conf['repos'].update(gerrit_prefix_list('operations/software/tools-'))
        conf['repos'].update(gerrit_prefix_list('cloud/toolforge/'))

        # custom horizon panels, but not upstream code
        conf['repos'].update(gerrit_prefix_list('openstack/horizon/wmf-'))

    dirname = f'hound-{name}'
    directory = os.path.join(DATA, dirname)
    if not os.path.isdir(directory):
        os.mkdir(directory)
    dest = os.path.join(directory, 'config.json')
    if os.path.exists(dest):
        with open(dest) as f:
            old = extract_urls(json.load(f))
    else:
        old = set()
    new = extract_urls(conf)
    # Write the new config always, in case names or other stuff changed
    print(f'{dirname}: writing new config')
    with open(dest, 'w') as f:
        json.dump(conf, f, indent='\t')
    if args.restart:
        if new != old:
            try:
                subprocess.check_call(['systemctl', 'status', dirname])
            except subprocess.CalledProcessError:
                print(f'{dirname}: not in systemd yet, skipping restart')
                return
            print(f'{dirname}: restarting...')
            subprocess.check_call(['systemctl', 'restart', dirname])
        else:
            print(f'{dirname}: skipping restart')


def extract_urls(conf) -> set:
    """extract a set of unique URLs from the config"""
    return {repo['url'] for repo in conf['repos'].values()}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description='Generate hound configuration')
    parser.add_argument('--restart', help='Restart hound instances if necessary',
                        action='store_true')
    return parser.parse_args(args=argv)


def main():
    args = parse_args()
    # "Search" profile should include everything unless there's a good reason
    make_conf('search', args,
              core=True,
              exts=True,
              skins=True,
              ooui=True,
              operations=True,
              puppet=True,
              # A dead codebase used by just one person
              armchairgm=False,
              twn=True,
              # FIXME: Justify
              milkshake=False,
              # All of these should already be included via core/exts/skins
              bundled=False,
              # Avoiding upstream libraries; to reconsider, see T227704
              vendor=False,
              # All of these should already be included via core/exts/skins
              wikimedia=False,
              pywikibot=True,
              services=True,
              libs=True,
              analytics=True,
              wmcs=True,
              # Heavily duplicates MediaWiki core + extensions
              shouthow=False,
              schemas=True,
              )

    make_conf('core', args, core=True)
    make_conf('pywikibot', args, pywikibot=True)
    make_conf('extensions', args, exts=True)
    make_conf('skins', args, skins=True)
    make_conf('things', args, exts=True, skins=True)
    make_conf('ooui', args, ooui=True)
    make_conf('operations', args, operations=True, puppet=True)
    make_conf('armchairgm', args, armchairgm=True)
    make_conf('milkshake', args, milkshake=True)
    make_conf('bundled', args, core=True, bundled=True, vendor=True)
    make_conf('deployed', args, core=True, wikimedia=True, vendor=True, services=True)
    make_conf('services', args, services=True)
    make_conf('libraries', args, ooui=True, milkshake=True, libs=True)
    make_conf('analytics', args, analytics=True)
    make_conf('wmcs', args, wmcs=True)
    make_conf('puppet', args, puppet=True)
    make_conf('shouthow', args, shouthow=True)


if __name__ == '__main__':
    main()
