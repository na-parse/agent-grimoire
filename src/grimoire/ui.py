'''Server-rendered HTML views for the human operator.

No JavaScript framework: pages are plain HTML built from f-strings and post back
through ordinary HTML forms. A few lines of script refresh the page, open the
compose dialog and fold long messages; everything still reads without it.

Pages: overview (``/ui``), agents, channels (list and one channel) and files.
'''

from __future__ import annotations

import re
from collections.abc import Collection
from datetime import UTC, datetime
from html import escape
from typing import Any
from urllib.parse import quote

from .config import BODY_MAX

# `file:<id>` references in message bodies become download links
FILE_REF = re.compile(r'\bfile:([a-z0-9]{8})\b')
# words with an underscore, which may be session names to link in message bodies
NAME_REF = re.compile(r'[\w-]*_[\w-]*')
# fenced code blocks; the capture group keeps the code when splitting a body
FENCE = re.compile(r'```[^\n]*\n(.*?)\n?```\n?', re.S)
# runs of lines starting with `|` (plain-text tables) keep their columns
TABLE = re.compile(r'((?:^\|.*(?:\n|$))+)', re.M)
INLINE_CODE = re.compile(r'`([^`\n]+)`')
PREVIEW_CHARS = 60
STATUS_CHARS = 48
# overview feed length; older messages live on the channel pages
OVERVIEW_MESSAGES = 15
RAIL_ROWS = 6
# files expiring sooner than this are highlighted
EXPIRY_SOON = 24 * 3600
# presence state -> glyph; shape and word carry the state, colour only adds to it
STATE_GLYPH = {'listening': '◉', 'active': '●', 'stale': '◐', 'offline': '○'}
STATE_ORDER = ('listening', 'active', 'stale', 'offline')
# the overview rail lists sessions that may need a look first
RAIL_ORDER = ('stale', 'listening', 'active')
# a trailing "(notice #N)" in a status; the rail's notice pill already says it
NOTICE_TAIL = re.compile(r'\s*\(notice #\d+\)\s*$')
# a project with this many stale sessions shows them as one rail line
STALE_GROUP = 3
# open notices show the most pressing first; each priority has a visible word
PRIORITY_LABEL = {
    'urgent': 'Urgent', 'high': 'High', 'default': 'Normal', 'low': 'Low',
    'min': 'Minimal',
}
TABS = (
    ('overview', '/ui', 'Overview'),
    ('agents', '/ui/agents', 'Agents'),
    ('channels', '/ui/channels', 'Channels'),
    ('files', '/ui/files', 'Files'),
)
CHANNEL_SORTS = {
    'recent': ('Latest', lambda c: c['last_id'], True),
    'name': ('Name', lambda c: c['name'], False),
    'messages': ('Messages', lambda c: c['messages'], True),
    'agents': ('Agents', lambda c: c['agents'], True),
}
FILE_SORTS = {
    'newest': ('Newest', lambda f: f['created_at'], True),
    'name': ('Name', lambda f: f['name'].lower(), False),
    'size': ('Size', lambda f: f['size'], True),
    'expires': ('Expiring', lambda f: f['expires_at'], False),
}

# =============================================================================
# Layout
# =============================================================================

STYLE = '''
:root { color-scheme: light dark;
  --paper: #f6f7f9; --card: #fff; --ink: #1c2433; --muted: #566072;
  --rule: #d8dce3; --edge: #858ea0; --soft: #eceff3; --accent: #0060c0;
  --listening: #0072b2; --active: #008a65; --stale: #a87300; --offline: #7d8698;
  --urgent: #b84a00; --high: #a87300; --default: #0060c0; --low: #7d8698;
  --on-badge: #fff;
  --serif: 'New York', 'Iowan Old Style', Charter, 'Bitstream Charter', Georgia,
           serif;
  --mono: ui-monospace, 'SF Mono', Menlo, Consolas, monospace; }
@media (prefers-color-scheme: dark) { :root {
  --paper: #121822; --card: #1a2230; --ink: #e6e9ef; --muted: #9ba5b5;
  --rule: #2d3747; --edge: #6f7a8d; --soft: #222b3a; --accent: #6cb2ff;
  --listening: #56b4e9; --active: #2fc08f; --stale: #e69f00; --offline: #7d8698;
  --urgent: #ef7b3b; --high: #e69f00; --default: #6cb2ff; --low: #7d8698;
  --on-badge: #0d1420; } }
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body { margin: 0; font: 15px/1.5 system-ui, -apple-system, 'Segoe UI', sans-serif;
       color: var(--ink); background: var(--paper);
       font-variant-numeric: tabular-nums; }
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
/* agent names everywhere: ink with a faint underline, accent on hover */
:root { --faint-line: color-mix(in srgb, currentColor 55%, transparent); }
a[href^="/ui/agents#"] { color: var(--ink);
  text-decoration: underline 1px var(--faint-line); text-underline-offset: 4px; }
a[href^="/ui/agents#"]:has(.agent) { text-decoration: none; }
a[href^="/ui/agents#"] .agent b { text-decoration: underline 1px var(--faint-line);
                                  text-underline-offset: 4px; }
a[href^="/ui/agents#"]:hover, a[href^="/ui/agents#"]:hover .agent b {
  color: var(--accent); text-decoration-color: currentColor; }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px;
                 border-radius: 3px; }
.top { background: var(--card); border-bottom: 1px solid var(--rule);
       position: sticky; top: 0; z-index: 5; }
.bar { max-width: 1180px; margin: 0 auto; padding: 0 20px; display: flex;
       align-items: center; gap: 28px; }
.mark { display: flex; align-items: center; gap: 9px; color: var(--ink);
        font: 600 21px/1 var(--serif); letter-spacing: .005em; padding: 12px 0;
        white-space: nowrap; }
.mark:hover { text-decoration: none; }
.mark svg { width: 26px; height: 26px; flex: none; }
.tabs { display: flex; gap: 4px; overflow-x: auto; scrollbar-width: none; }
.tabs a { color: var(--muted); padding: 16px 10px 13px; white-space: nowrap;
          border-bottom: 3px solid transparent; font-weight: 500; }
.tabs a:hover { color: var(--ink); text-decoration: none; }
.tabs a[aria-current] { color: var(--ink); border-bottom-color: var(--accent); }
.count { display: inline-block; min-width: 21px; padding: 0 6px; margin-left: 5px;
         border-radius: 11px; background: var(--urgent); color: var(--on-badge);
         font-size: 13px; line-height: 21px; text-align: center; font-weight: 700; }
main { max-width: 1180px; margin: 0 auto; padding: 20px 20px 40px; }
h1, h2 { font-family: var(--serif); font-weight: 600; letter-spacing: .005em; }
h1 { font-size: 26px; line-height: 1.2; margin: 4px 0 4px; }
h2 { font-size: 19px; line-height: 1.3; margin: 0 0 10px; }
.lede { color: var(--muted); margin: 0; text-wrap: balance; }
.head { display: flex; align-items: baseline; justify-content: space-between;
        gap: 8px 16px; flex-wrap: wrap; margin-bottom: 14px; }
.head h1, .head h2 { margin: 0; }
.tools { display: flex; gap: 14px; align-items: center; flex-wrap: wrap;
         font-size: 14px; color: var(--muted); }
.tools a, .more a, footer a, .who-here a { display: inline-block;
                                             padding-block: 4px; }
/* inline padding widens the tap target without moving the baseline */
td a { padding-block: 5px; }
td .chan { white-space: nowrap; }
section + section { margin-top: 30px; }
.panel { background: var(--card); border: 1px solid var(--rule); border-radius: 10px; }
.empty { color: var(--muted); padding: 14px 16px; margin: 0; }
.more { color: var(--muted); font-size: 14px; margin: 10px 2px 0; }
button, .button { font: inherit; font-weight: 600; font-size: 14px; cursor: pointer;
  border-radius: 7px; padding: 7px 14px; border: 1px solid var(--accent);
  background: var(--accent); color: var(--on-badge); line-height: 1.4; }
button.quiet { background: transparent; color: var(--accent); }
button.link { background: transparent; color: var(--accent); border-color: transparent;
              padding: 2px 8px; font-weight: 500; vertical-align: baseline; }
button.link:hover, button.link:focus-visible { border-color: var(--accent); }
button.link[aria-pressed="true"] { background: var(--soft); border-color: var(--edge);
                                   color: var(--ink); }

/* overview: notices and feed beside a summary rail */
.overview { display: grid; gap: 28px; grid-template-columns: minmax(0, 1fr) 300px;
            grid-template-areas: 'notices rail' 'feed rail'; align-items: start; }
.overview > .notices { grid-area: notices; } .overview > .feed { grid-area: feed; }
.overview > .rail { grid-area: rail; position: sticky; top: 76px; }
.overview > section + section { margin-top: 0; }
@media (max-width: 900px) {
  .overview { grid-template-columns: minmax(0, 1fr); gap: 24px;
              grid-template-areas: 'notices' 'rail' 'feed'; }
  .overview > .rail { position: static; } }
.rail .panel { padding: 14px 16px; }
.rail h2 { font-size: 16px; margin: 0 0 6px; }
.rail h2 a { color: var(--ink); }
.rail .block + .block { border-top: 1px solid var(--rule); margin-top: 12px;
                        padding-top: 12px; }
.tally { display: flex; flex-wrap: wrap; gap: 2px 14px; margin: 0; padding: 0;
         list-style: none; font-size: 14px; }
.mini { list-style: none; margin: 8px 0 0; padding: 0; font-size: 14px; }
.mini li { display: flex; justify-content: space-between; align-items: baseline;
           gap: 10px; padding: 3px 0; }
.mini li > :first-child { overflow: hidden; text-overflow: ellipsis;
                          white-space: nowrap; min-width: 0; }
.mini .dim { color: var(--muted); white-space: nowrap; font-size: 13px; }
.mini .say { display: block; color: var(--muted); font-size: 13px;
             overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.mini .agent { display: block; padding: 2px 0; }
.mini .agent b { font-weight: 600; }
.mini .group a, .mini .group .agent b { font-weight: 400; color: var(--muted); }
.mini .flags { white-space: nowrap; }
.mini li:has(> .say) { display: grid; grid-template-columns: minmax(0, 1fr) auto;
                       gap: 0 10px; }
.mini li > .say { grid-column: 1 / -1; }
.mini .flags .flag { margin: 0 0 0 4px; }
.block .foot { font-size: 14px; margin-top: 4px; }
@media (max-width: 900px) { .rail .panel { display: grid; gap: 14px 22px;
    grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); }
  .rail .block + .block { border-top: 0; margin-top: 0; padding-top: 0; } }
@media (min-width: 601px) and (max-width: 900px) {
  .rail .panel { grid-template-columns: 1fr 1fr; align-content: start;
                 grid-template-areas: 'agents chans' 'agents files';
                 grid-template-rows: auto 1fr; }
  .b-agents { grid-area: agents; } .b-chans { grid-area: chans; }
  .b-files { grid-area: files; } }
.mini .flag { display: inline-block; font-size: 12px; font-weight: 600;
              padding: 3px 7px; margin: 1px 6px 1px 0; border-radius: 10px;
              border: 1px solid var(--pri); color: var(--ink);
              line-height: 16px; }
.mini .say { padding-top: 1px; }

/* notices: bookmark ribbon in the priority colour, priority named in words */
.notice { position: relative; background: var(--card); border: 1px solid var(--rule);
          border-radius: 4px 10px 10px 4px; padding: 12px 16px 12px 40px;
          margin: 0 0 10px; scroll-margin-top: 100px; }
.notice::before { content: ''; position: absolute; left: 12px; top: -1px;
  width: 16px; height: 34px; background: var(--pri);
  clip-path: polygon(0 0, 100% 0, 100% 100%, 50% 74%, 0 100%); }
.pri-urgent { --pri: var(--urgent); } .pri-high { --pri: var(--high); }
.pri-default { --pri: var(--default); } .pri-low, .pri-min { --pri: var(--low); }
.notice.pri-urgent { border-color: var(--urgent);
                     box-shadow: inset 3px 0 0 var(--urgent); }
.notice .pri { font-weight: 700; color: var(--ink); }
.notice.done { background: transparent; color: var(--muted); }
.notice.done::before { background: var(--edge); }
.notice .resolution { margin-top: 6px; font-size: 14px; }
.calm { padding: 12px 16px; color: var(--muted); background: var(--card);
        border: 1px dashed var(--edge); border-radius: 10px; margin: 0; }
details.resolved > summary { cursor: pointer; color: var(--muted); padding: 6px 0;
                             margin-bottom: 6px; font-size: 14px; }

/* messages */
.feed .panel { padding: 0 16px; }
.msg { padding: 13px 0; border-top: 1px solid var(--rule); scroll-margin-top: 100px; }
.msg:first-child { border-top: 0; }
.msg.op { margin: 0 -16px; padding: 13px 16px 13px 13px;
          border-left: 3px solid var(--accent); background: var(--soft); }
.meta { display: flex; flex-wrap: wrap; align-items: baseline; gap: 2px 10px;
        font-size: 13px; color: var(--muted); }
.meta .who { color: var(--ink); font-weight: 600; font-size: 14px;
             overflow-wrap: anywhere; }
.meta .to { overflow-wrap: anywhere; }
.meta .where { display: contents; }
.meta .stamp { margin-left: auto; color: var(--muted); white-space: nowrap;
               padding: 2px 0; }
.chan { font-weight: 500; }
.body { white-space: pre-wrap; overflow-wrap: anywhere; margin-top: 5px; }
.body pre { font: 13px/1.45 var(--mono); background: var(--soft); margin: 6px 0;
            padding: 8px 10px; border-radius: 6px; overflow-x: auto;
            white-space: pre; overflow-wrap: normal; }
.body a, .meta a { padding-block: 4px; }
.body:focus-visible { outline-width: 1px; outline-offset: 4px; }
.body code { font: .9em var(--mono); background: var(--soft); padding: 1px 4px;
             border-radius: 4px; }
.body.folded { max-height: 15em; overflow: hidden;
  -webkit-mask-image: linear-gradient(#000 70%, transparent);
  mask-image: linear-gradient(#000 70%, transparent); }
.unfold { margin-top: 2px; padding: 5px 0; border: 0; background: none;
          color: var(--accent); font-size: 14px; font-weight: 500; }
.flash { background: var(--card); border: 1px solid var(--urgent);
         box-shadow: inset 3px 0 0 var(--urgent); border-radius: 8px;
         padding: 10px 14px; margin: 0 0 16px; }

/* presence */
.st { white-space: nowrap; font-size: 13px; color: var(--ink); }
.st i { font-style: normal; margin-right: 4px; }
.st-listening i { color: var(--listening); } .st-active i { color: var(--active); }
.st-stale i { color: var(--stale); } .st-offline i { color: var(--offline); }
.st-offline { color: var(--muted); }
.legend { display: flex; flex-wrap: wrap; gap: 4px 18px; margin: 8px 0 0;
          padding: 0; list-style: none; font-size: 13px; color: var(--muted); }
details.states { display: none; font-size: 13px; color: var(--muted);
                 margin-top: 4px; }
details.states > summary { cursor: pointer; padding: 4px 0; }
.who-here { display: flex; flex-wrap: wrap; gap: 0 16px; font-size: 14px;
            margin: 6px 0 0; padding: 0; list-style: none; }


/* tables (agents, files): rows stack into compact cards on narrow screens */
table { width: 100%; border-collapse: collapse; font-size: 14px; }
th { text-align: left; font-weight: 600; color: var(--muted); font-size: 13px;
     padding: 10px 12px; border-bottom: 1px solid var(--rule); white-space: nowrap; }
td { padding: 11px 12px; border-top: 1px solid var(--rule); vertical-align: top; }
tbody tr:first-child td { border-top: 0; }
td.num, th.num { text-align: right; }
td time, .chans time { white-space: nowrap; }
td.who { min-width: 210px; max-width: 320px; }
td.status { max-width: 300px; }
.id { overflow-wrap: normal; }
tr.group th { font: 600 16px/1.3 var(--serif); color: var(--ink);
              background: var(--soft); padding: 9px 12px; border-top: 1px solid
              var(--rule); border-bottom: 0; }
tr.group th span { font: 400 13px system-ui, sans-serif; color: var(--muted);
                   margin-left: 8px; }
.name { font-weight: 600; overflow-wrap: break-word; }
.sub { color: var(--muted); font-size: 13px; overflow-wrap: anywhere; }
tr.gone td { color: var(--muted); }
tr:target td { background: var(--soft); }
tr[id] { scroll-margin-top: 100px; }
td.soon { font-weight: 700; white-space: nowrap; }
.tag { display: inline-block; font-size: 12px; font-weight: 600; padding: 0 6px;
       margin-left: 6px; border-radius: 4px; border: 1px solid var(--edge);
       color: var(--ink); line-height: 18px; }
@media (max-width: 1000px) {
  thead { display: none; }
  table, tbody, tr.group, tr.group th { display: block; }
  /* each row becomes a compact card: title line, details, then a meta line */
  tbody tr:not(.group) { display: grid; grid-template-columns: auto auto 1fr auto;
    gap: 2px 16px; padding: 12px 14px; border-top: 1px solid var(--rule);
    align-items: baseline; }
  tbody tr:first-child { border-top: 0; }
  td { display: block; padding: 0; border: 0; min-width: 0; max-width: none; }
  td.full { grid-column: 1 / -1; }
  td.num { text-align: left; }
  td.inline[data-label]::before, td.label[data-label]::before {
    content: attr(data-label) ' '; color: var(--muted); }
  td.inline { font-size: 13px; white-space: nowrap; min-width: max-content; }
  /* agents: host + state, status, channels, then messages, last call, action */
  .agents td.who { grid-area: 1 / 1 / 2 / 4; }
  .agents td.state { grid-area: 1 / 4; }
  .agents td:nth-child(3) { grid-row: 2; }
  .agents td:nth-child(4) { grid-row: 3; }
  .agents td.inline { grid-row: 4; }
  .agents td.act { grid-area: 4 / 4; align-self: baseline; }
  .agents td.act button { margin-right: -8px; }
  .agents .proj { display: none; }
  td.status { max-width: 70ch; }
  /* files: name, then size, uploaded and expiry on one line, then uploader */
  .files td:first-child { grid-row: 1; }
  .files td.inline { grid-row: 2; }
  .files td.from { grid-row: 4; }
  .files td.soon { grid-row: 3; grid-column: 1 / -1; }
  .agents td.state, .agents td.act { justify-self: end; }
  .legend .what { display: none; }
  .legend { gap: 2px 14px; }
  details.states { display: block; }
  details.states .legend { display: grid; gap: 4px; }
  }
/* tablets: agent cards two to a row under each project header */
@media (min-width: 601px) and (max-width: 1000px) {
  .agents tbody { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .agents tr.group { grid-column: 1 / -1; }
  .agents tbody tr:nth-child(odd):not(.group) { border-left: 1px solid var(--rule); }
  }

/* channels */
.chans { list-style: none; margin: 0; padding: 0 16px; }
.chans > li { padding: 13px 0; border-top: 1px solid var(--rule); }
.chans > li:first-child { border-top: 0; }
.chans .title { display: flex; flex-wrap: wrap; align-items: baseline;
                gap: 2px 14px; }
.chans .title a { font-weight: 600; font-size: 16px; padding-block: 2px; }
.chans .title time { margin-left: auto; color: var(--muted); font-size: 13px; }
.chans .facts { color: var(--muted); font-size: 13px; }
.preview { list-style: none; margin: 6px 0 0; padding: 0; font-size: 14px; }
.preview li { overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
              color: var(--muted); }
.preview b { color: var(--ink); font-weight: 600; }
@media (max-width: 600px) { .preview li { white-space: normal; display: -webkit-box;
    -webkit-line-clamp: 2; -webkit-box-orient: vertical; }
  .preview li + li { margin-top: 4px; } }
.sorts { display: flex; gap: 4px; align-items: center; flex-wrap: wrap;
         margin: 0 0 12px;
         font-size: 14px; color: var(--muted); white-space: nowrap; }
.sorts a, .sorts b { padding: 4px 10px; border-radius: 6px; }
.sorts b { background: var(--card); border: 1px solid var(--rule); color: var(--ink);
           font-weight: 600; }

/* storage */
.meter { height: 12px; border-radius: 6px; background: var(--soft); overflow: hidden;
         border: 1px solid var(--edge); margin: 8px 0 4px; }
.meter span { display: block; height: 100%; background: var(--accent);
              min-width: 6px; }
.meter.warn span { background: var(--urgent); }
.usage { display: flex; justify-content: space-between; gap: 2px 12px;
         flex-wrap: wrap; font-size: 14px; color: var(--muted); }
.usage b { color: var(--ink); }
.storage { padding: 14px 16px; margin-bottom: 20px; }

/* compose dialog */
dialog { border: 1px solid var(--rule); border-radius: 12px; padding: 0;
         width: min(560px, calc(100vw - 24px)); background: var(--card);
         color: var(--ink); }
dialog::backdrop { background: rgb(10 15 25 / .45); }
dialog form { padding: 18px 20px 20px; }
dialog h2 { margin-bottom: 14px; }
label { display: block; font-size: 14px; font-weight: 600; margin-bottom: 12px; }
label small { font-weight: 400; color: var(--muted); }
input, textarea { display: block; width: 100%; margin-top: 4px; font: inherit;
  font-weight: 400; padding: 8px 10px; border: 1px solid var(--edge);
  border-radius: 7px; background: var(--paper); color: var(--ink); }
textarea { resize: vertical; min-height: 120px; }
.actions { display: flex; justify-content: flex-end; gap: 10px; }
footer { max-width: 1180px; margin: 0 auto; padding: 0 20px 32px; font-size: 14px;
         color: var(--muted); display: flex; flex-wrap: wrap; gap: 4px 16px;
         align-items: center; justify-content: space-between; }
@media (max-width: 600px) {
  .bar { gap: 6px; flex-wrap: wrap; padding: 0 12px; }
  .mark { padding: 10px 0 0; font-size: 19px; }
  .tabs { width: 100%; margin: 0 -10px; }
  .tabs a { padding: 9px 10px 9px; }
  main { padding: 16px 12px 32px; }
  footer { padding: 0 12px 28px; }
  h1 { font-size: 23px; }
  .feed .panel, .chans { padding: 0 14px; }
  .msg.op { margin: 0 -14px; padding-left: 11px; }
  .notice { padding: 12px 14px 12px 34px; }
  .notice::before { left: 10px; width: 13px; height: 28px; }
  .notice .stamp { margin-left: 0; }
  .msg .meta .where.to-many { display: flex; flex-wrap: wrap; gap: 0 10px; order: 2;
                      flex-basis: 100%; }
  .rail .chan-list li:nth-child(n+5) { display: none; } }
@media (prefers-reduced-motion: reduce) { * { scroll-behavior: auto !important; } }
'''

# Tome with a four-point star on an azure tile; chunky shapes so it reads at 16px.
FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
    '<rect width="64" height="64" rx="14" fill="#0060c0"/>'
    '<rect x="15" y="9" width="36" height="46" rx="4" fill="#fff"/>'
    '<rect x="15" y="9" width="8" height="46" rx="3" fill="#b9d6f2"/>'
    '<path d="M37 19l3 10 10 3-10 3-3 10-3-10-10-3 10-3z" fill="#0060c0"/>'
    '</svg>'
)

SCRIPT = '''
// Show a flash banner once: drop it from the URL so auto-refresh clears it.
const u = new URL(location.href);
if (u.searchParams.has('flash')) {
  u.searchParams.delete('flash');
  history.replaceState(null, '', u);
}
const compose = document.getElementById('compose');
// Compose buttons can preset the recipient and channel (agents page).
document.querySelectorAll('[data-compose]').forEach((b) => b.addEventListener(
  'click', () => {
    for (const k of ['to', 'channel']) {
      if (b.dataset[k] !== undefined) compose.querySelector(`[name=${k}]`).value =
        b.dataset[k];
    }
    compose.showModal();
    compose.querySelector(b.dataset.to ? 'textarea' : 'input').focus();
  }));
document.querySelectorAll('[data-close]').forEach((b) => b.addEventListener(
  'click', () => b.closest('dialog').close()));
// Fold very long messages; a button shows the rest.
document.querySelectorAll('.body').forEach((el) => {
  if (el.scrollHeight < (innerWidth < 600 ? 280 : 400)) return;
  el.classList.add('folded');
  const more = document.createElement('button');
  more.className = 'unfold';
  more.textContent = 'Show full message';
  more.onclick = () => {
    el.classList.replace('folded', 'unfolded');
    el.tabIndex = -1;
    el.focus({ preventScroll: true });
    more.remove();
  };
  el.after(more);
});
// Auto-refresh unless paused, hidden, or the operator is reading or writing:
// an open dialog or section, an unfolded message, selected text or a draft.
const KEY = 'grimoire-refresh-paused';
const toggle = document.getElementById('refresh');
const paused = () => localStorage.getItem(KEY) === '1';
const note = document.getElementById('refresh-note');
const show = () => {
  toggle.setAttribute('aria-pressed', String(paused()));
  note.textContent = paused() ? 'Auto-refresh is paused.' : note.dataset.on;
};
toggle.onclick = () => { localStorage.setItem(KEY, paused() ? '0' : '1'); show(); };
show();
setInterval(() => {
  if (paused() || document.hidden || String(getSelection())) return;
  // keyboard users keep their place: no reload while focus is in the page
  const f = document.activeElement;
  if (f && f !== document.body && f.matches(':focus-visible')) return;
  if (document.querySelector('dialog[open], main details[open], .unfolded')) return;
  const t = document.querySelector('textarea');
  if (t && t.value) return;
  location.reload();
}, REFRESH);
'''


def _page(
    title: str,
    tab: str,
    main: str,
    open_notices: int = 0,
    compose: str = '',
    page: str = '',
    refresh: int = 20,
) -> str:
    '''Wrap content in the shared layout: header with tabs, then ``main``.

    The browser title is ``page`` if given, else the tab name and site ``title``.
    '''
    links = []
    for key, href, label in TABS:
        current = ' aria-current="page"' if key == tab else ''
        badge = (
            f'<span class="count"><span class="sr"> (</span>{open_notices}'
            f'<span class="sr"> open notices)</span></span>'
            if key == 'overview' and open_notices else ''
        )
        links.append(f'<a href="{href}"{current}>{label}{badge}</a>')
    prefix = f'({open_notices}) ' if open_notices else ''
    label = dict((k, v) for k, _, v in TABS)[tab]
    heading = title if tab == 'overview' else f'{label} · {title}'
    mark = FAVICON_SVG.replace('<svg ', '<svg aria-hidden="true" ', 1)
    script = SCRIPT.replace('REFRESH', str(refresh * 1000))
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{prefix}{escape(page or heading)}</title>
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<style>{STYLE}
.sr {{ position: absolute; width: 1px; height: 1px; overflow: hidden;
       clip-path: inset(50%); white-space: nowrap; }}</style></head>
<body><header class="top"><div class="bar">
<a class="mark" href="/ui">{mark}{escape(title)}</a>
<nav class="tabs" aria-label="Pages">{"".join(links)}</nav></div></header>
<main>{main}</main>
<footer><span>Agents follow the <a href="/">coordination guide</a>.</span>
<span><span id="refresh-note" data-on="Updates every {refresh} seconds."
 aria-live="polite">Updates every {refresh} seconds.</span>
<button type="button" class="link" id="refresh" aria-pressed="false">Pause
auto-refresh</button></span></footer>
{compose}<script>{script}</script></body></html>'''


# =============================================================================
# Formatting
# =============================================================================


def _stamp(iso: str) -> str:
    '''Absolute UTC time for tooltips and screen readers.'''
    return datetime.fromisoformat(iso).strftime('%Y-%m-%d %H:%M UTC')


def _span(iso: str, now: datetime | None = None) -> tuple[str, bool]:
    '''Compact distance from now (``5m``) and whether ``iso`` is in the future.'''
    secs = int(((now or datetime.now(UTC)) - datetime.fromisoformat(iso))
               .total_seconds())
    future, secs = secs < 0, abs(secs)
    for size, unit in ((86400, 'd'), (3600, 'h'), (60, 'm'), (1, 's')):
        if secs >= size or unit == 's':
            return f'{secs // size}{unit}', future
    return '', future


def _ago(iso: str | None, now: datetime | None = None) -> str:
    '''``<time>`` showing how long ago (or, if future, how soon) ``iso`` is.'''
    if not iso:
        return ''
    span, future = _span(iso, now)
    label = f'in {span}' if future else f'{span} ago'
    return f'<time datetime="{escape(iso)}" title="{_stamp(iso)}">{label}</time>'


def _size(n: int) -> str:
    '''Human-readable byte count (1000-based, like disk vendors and Finder).'''
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1000 or unit == 'TB':
            return f'{n} {unit}' if unit == 'B' else f'{n:.1f} {unit}'
        n /= 1000  # type: ignore[assignment]
    return ''


def _plural(n: int, word: str) -> str:
    '''``n`` and ``word``, pluralised with a trailing s.'''
    return f'{n} {word}{"" if n == 1 else "s"}'


def _state(state: str) -> str:
    '''Presence badge: glyph (shape) plus word, coloured by state.'''
    return (
        f'<span class="st st-{state}"><i aria-hidden="true">'
        f'{STATE_GLYPH.get(state, "●")}</i>{state}</span>'
    )


def _name(name: str) -> str:
    '''Escaped session name that may wrap after its underscores.'''
    return escape(name).replace('_', '_<wbr>')


def _short(name: str) -> str:
    '''Session name without its project prefix (``<host>_<rand>``) for tight spots.'''
    parts = name.split('_')
    return '_'.join(parts[1:]) if len(parts) >= 3 else name


def _agent_link(name: str, label: str = '') -> str:
    '''Link to a session's row on the agents page (``label`` defaults to the name).'''
    return (
        f'<a href="/ui/agents#a-{quote(name)}" title="{escape(name)}">'
        f'{label or escape(_short(name))}</a>'
    )


def _chan_link(name: str) -> str:
    '''Link to one channel's page.'''
    return f'<a class="chan" href="/ui/channels/{quote(name)}">#{escape(name)}</a>'


def _rich(body: str, names: Collection[str] = ()) -> str:
    '''Escape a message body, then format code fences, tables and links.

    Escaping comes first, so agents can never inject HTML; only the markup added
    here is live. Session ``names`` outside code blocks become short agent links.
    '''

    def name_link(m: re.Match[str]) -> str:
        return _agent_link(m[0]) if m[0] in names else m[0]

    parts = FENCE.split(escape(body))
    out = []
    for i, part in enumerate(parts):
        if i % 2:
            out.append(f'<pre>{part}</pre>')
            continue
        part = TABLE.sub(lambda m: f'<pre>{m[1].rstrip(chr(10))}</pre>', part)
        bits = INLINE_CODE.split(part)
        part = ''.join(
            f'<code>{b}</code>' if j % 2 else NAME_REF.sub(name_link, b)
            for j, b in enumerate(bits)
        )
        out.append(FILE_REF.sub(r'<a href="/api/files/\1">file:\1</a>', part))
    return ''.join(out)


def _excerpt(body: str, limit: int = PREVIEW_CHARS) -> str:
    '''First ``limit`` characters of a body on one line.'''
    flat = ' '.join(body.split())
    return flat if len(flat) <= limit else f'{flat[:limit]}…'


# =============================================================================
# Components
# =============================================================================


def _notice(n: dict[str, Any], names: Collection[str] = ()) -> str:
    '''Render an operator notice: a bookmark ribbon in its priority colour.'''
    pri = n['priority']
    label = PRIORITY_LABEL.get(pri, pri.title())
    done = ''
    if n['resolved_at']:
        note = f': {escape(n["note"])}' if n['note'] else ''
        done = (
            f'<div class="resolution">Resolved by {escape(n["resolved_by"])} '
            f'{_ago(n["resolved_at"])}{note}</div>'
        )
    cls = f'notice pri-{pri}' + (' done' if n['resolved_at'] else '')
    return (
        f'<article class="{cls}" id="n{n["id"]}"><div class="meta">'
        f'<span class="pri">{label}</span>'
        f'<span class="who">{_agent_link(n["agent"])}</span>'
        f'<span>notice {n["id"]}</span>'
        f'<span class="stamp">{_ago(n["created_at"])}</span></div>'
        f'<div class="body">{_rich(n["body"], names)}</div>{done}</article>'
    )


def _notices(notices: list[dict[str, Any]], names: Collection[str] = ()) -> str:
    '''"Needs you" section: open notices, most pressing first, then resolved ones.'''
    rank = {p: i for i, p in enumerate(PRIORITY_LABEL)}
    open_ = sorted(
        (n for n in notices if not n['resolved_at']),
        key=lambda n: rank.get(n['priority'], 2),
    )
    done = [n for n in notices if n['resolved_at']]
    body = ''.join(_notice(n, names) for n in open_) or (
        '<p class="calm">Nothing needs you right now. Agents raise notices here '
        'when they need a decision.</p>'
    )
    if done:
        body += (
            f'<details class="resolved"><summary>{len(done)} resolved</summary>'
            f'{"".join(_notice(n, names) for n in done)}</details>'
        )
    count = f' ({len(open_)})' if open_ else ''
    return f'<section class="notices"><h1>Needs you{count}</h1>{body}</section>'


def _message(
    msg: dict[str, Any], channel: bool = True, names: Collection[str] = ()
) -> str:
    '''Render one board message (``channel``: show which channel it is in).'''
    to = (
        f'<span class="to">to {", ".join(_agent_link(n) for n in msg["recipients"])}'
        '</span>'
        if msg['recipients'] else ''
    )
    where = _chan_link(msg['channel']) if channel else ''
    op = msg['agent'] == 'operator'
    cls = 'msg op' if op else 'msg'
    sender = 'operator' if op else _agent_link(msg['agent'])
    span, future = _span(msg['created_at'])
    seen = f'in {span}' if future else f'{span} ago'
    return (
        f'<article class="{cls}" id="m{msg["id"]}"><div class="meta">'
        f'<span class="who">{sender}</span>'
        f'<span class="where{" to-many" if to else ""}">{to}{where}</span>'
        f'<a class="stamp" href="#m{msg["id"]}" aria-label="{seen}, message '
        f'{msg["id"]}, {_stamp(msg["created_at"])}">{_ago(msg["created_at"])}</a>'
        '</div>'
        f'<div class="body">{_rich(msg["body"], names)}</div></article>'
    )


def _feed(
    messages: list[dict[str, Any]], channel: bool = True, names: Collection[str] = ()
) -> str:
    '''Messages newest first in one panel (known session ``names`` are linked).'''
    rows = ''.join(_message(m, channel, names) for m in reversed(messages))
    return f'<div class="panel">{rows or "<p class=empty>No messages yet.</p>"}</div>'


def _compose(
    channel: str, draft: dict[str, str] | None, channels: list[str]
) -> str:
    '''Operator compose dialog (``draft`` refills and opens it after an error).'''
    d = draft or {}
    options = ''.join(f'<option value="{escape(c)}">' for c in channels)
    shown = ' open' if draft else ''
    return f'''<dialog id="compose"{shown} aria-labelledby="compose-title">
<form method="post" action="/ui/post">
<h2 id="compose-title">Message agents</h2>
<label>Channel<input name="channel" list="channel-names" required
 value="{escape(d.get('channel', channel))}" autocomplete="off"></label>
<datalist id="channel-names">{options}</datalist>
<label>To <small>(optional: session names, comma-separated)</small><input name="to"
 value="{escape(d.get('to', ''))}" autocomplete="off"></label>
<label>Message<textarea name="body" rows="6" maxlength="{BODY_MAX}"
 required>{escape(d.get('body', ''))}</textarea></label>
<div class="actions"><button type="button" class="quiet" data-close>Cancel</button>
<button type="submit">Send as operator</button></div></form></dialog>'''


def _share(used: int, capacity: int) -> tuple[float, str]:
    '''Fraction of ``capacity`` in use and a label that never reads a false 0%.'''
    share = min(used / capacity, 1.0) if capacity else 1.0
    pct = share * 100
    if 0 < pct < 0.1:
        return share, 'under 0.1%'
    return share, f'{pct:.1f}%' if pct < 10 else f'{pct:.0f}%'


def _meter(used: int, capacity: int) -> str:
    '''Storage bar: bytes held by Grimoire against what it may use.'''
    share, pct = _share(used, capacity)
    warn = ' warn' if share >= 0.9 else ''
    width = f'{share * 100:.2f}%' if used else '0'
    return (
        f'<div class="meter{warn}" role="meter" aria-label="Storage used"'
        f' aria-valuemin="0" aria-valuemax="100" aria-valuenow="{share * 100:.2f}"'
        f' aria-valuetext="{_size(used)} of {_size(capacity)}, {pct}">'
        f'<span style="width: {width}"></span></div>'
    )


def _rail_agents(
    agents: list[dict[str, Any]], notices: list[dict[str, Any]]
) -> list[str]:
    '''Rail rows: sessions with open notices, then stale ones, then live ones.

    A project with ``STALE_GROUP`` or more stale sessions gets one summary row.
    '''
    asks: dict[str, list[dict[str, Any]]] = {}
    for n in notices:
        if not n['resolved_at']:
            asks.setdefault(n['agent'], []).append(n)
    rank = {s: i for i, s in enumerate(RAIL_ORDER)}
    urgency = {p: i for i, p in enumerate(PRIORITY_LABEL)}
    for held in asks.values():
        held.sort(key=lambda n: (urgency.get(n['priority'], 2), n['id']))

    def order(a: dict[str, Any]) -> tuple[int, int, int]:
        held = asks.get(a['name'])
        if held:
            return 0, urgency.get(held[0]['priority'], 2), held[0]['id']
        return 1, rank[a['state']], 0

    shown = sorted((a for a in agents if a['state'] in rank), key=order)
    stale: dict[str, int] = {}
    for a in shown:
        if a['state'] == 'stale' and a['name'] not in asks:
            stale[a['project']] = stale.get(a['project'], 0) + 1
    rows, grouped = [], set()
    for a in shown:
        project = a['project']
        if a['name'] not in asks and stale.get(project, 0) >= STALE_GROUP:
            if a['state'] == 'stale' and project not in grouped:
                grouped.add(project)
                rows.append(
                    f'<li class="group"><span><a href="/ui/agents"><span class="agent">'
                    f'<span class="st st-stale"><i aria-hidden="true">'
                    f'{STATE_GLYPH["stale"]}</i></span><b>{escape(project)}</b> '
                    f'({stale[project]} stale)</span></a></span></li>'
                )
            if a['state'] == 'stale':
                continue
        flag = ''.join(
            f'<a class="flag pri-{n["priority"]}" href="#n{n["id"]}">'
            f'{PRIORITY_LABEL.get(n["priority"], n["priority"].title())} '
            f'notice {n["id"]}</a>'
            for n in asks.get(a['name'], [])
        )
        right = f'<span class="flags">{flag}</span>' if flag else (
            f'<span class="dim">{_ago(a["last_seen"])}</span>'
        )
        rows.append(
            f'<li><span>{_agent_link(a["name"], _state_name(a))}</span>{right}'
            f'<span class="say">'
            f'{escape(_excerpt(NOTICE_TAIL.sub("", a["status"]), STATUS_CHARS))}'
            '</span></li>'
        )
    return rows


def _rail(
    agents: list[dict[str, Any]],
    notices: list[dict[str, Any]],
    channels: list[dict[str, Any]],
    files: int,
    used: int,
    capacity: int,
) -> str:
    '''Overview summary: agents needing a look first, channels, storage.'''
    counts = {s: sum(a['state'] == s for a in agents) for s in STATE_ORDER}
    tally = ''.join(
        f'<li>{_state(s)} <b>{n}</b></li>' for s, n in counts.items() if n
    ) or '<li class="sub">No sessions in the last day</li>'
    rows = _rail_agents(agents, notices)
    who = ''.join(rows[:RAIL_ROWS])
    more = len(rows) - RAIL_ROWS
    agents_foot = (
        f'<div class="foot"><a href="/ui/agents">{more} more on the Agents page</a>'
        '</div>' if more > 0 else ''
    )
    chans = ''.join(
        f'<li><span>{_chan_link(c["name"])}</span>'
        f'<span class="dim">{_plural(c["messages"], "msg")}, '
        f'{_ago(c["last_at"])}</span></li>' for c in channels[:RAIL_ROWS]
    )
    _, pct = _share(used, capacity)
    return f'''<aside class="rail" aria-label="Summary"><div class="panel">
<div class="block b-agents"><h2><a href="/ui/agents">Agents</a></h2>
<ul class="tally">{tally}</ul><ul class="mini">{who}</ul>{agents_foot}</div>
<div class="block b-chans"><h2><a href="/ui/channels">Channels</a></h2>
<ul class="mini chan-list">{chans}</ul>
<div class="foot"><a href="/ui/channels">All {_plural(len(channels), "channel")}</a>
</div></div>
<div class="block b-files"><h2><a href="/ui/files">Files</a></h2>
<div class="usage"><span><b>{_plural(files, "file")}</b>, {_size(used)}</span>
<span>{pct} of {_size(capacity)}</span></div>
{_meter(used, capacity)}</div></div></aside>'''


def _state_name(a: dict[str, Any]) -> str:
    '''Short session name led by its state glyph (state also in a hidden word).'''
    return (
        f'<span class="agent"><span class="st st-{a["state"]}"><i aria-hidden="true">'
        f'{STATE_GLYPH.get(a["state"], "●")}</i><span class="sr">{a["state"]} </span>'
        f'</span><b>{escape(_short(a["name"]))}</b></span>'
    )


# =============================================================================
# Pages
# =============================================================================


def render_overview(
    title: str,
    notices: list[dict[str, Any]],
    channels: list[dict[str, Any]],
    agents: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    files: int,
    used: int,
    capacity: int,
) -> str:
    '''Landing page: open notices, the newest messages and a summary rail.'''
    open_ = sum(not n['resolved_at'] for n in notices)
    recent = messages[-OVERVIEW_MESSAGES:]
    more = (
        f'<p class="more">Showing the {len(recent)} newest. Older messages are on '
        '<a href="/ui/channels">each channel\'s page</a>.</p>'
        if len(messages) > len(recent) else ''
    )
    names = {a['name'] for a in agents}
    feed = _feed(recent, True, names)
    main = f'''<div class="overview">{_notices(notices, names)}
{_rail(agents, notices, channels, files, used, capacity)}
<section class="feed"><div class="head"><h2>Recent messages</h2>
<button data-compose>New message</button></div>{feed}{more}</section></div>'''
    compose = _compose('', None, [c['name'] for c in channels])
    return _page(title, 'overview', main, open_, compose)


def render_channel(
    title: str,
    channel: dict[str, Any] | None,
    name: str,
    messages: list[dict[str, Any]],
    channels: list[dict[str, Any]],
    participants: list[dict[str, Any]],
    open_notices: int,
    flash: str = '',
    draft: dict[str, str] | None = None,
) -> str:
    '''One channel's messages, newest first, with who takes part and compose.

    ``flash`` is shown as a banner (post errors and recipient warnings).
    '''
    banner = f'<div class="flash" role="alert">{escape(flash)}</div>' if flash else ''
    facts = 'No messages yet'
    if channel:
        shown = (
            f', showing the newest {len(messages)}'
            if channel['messages'] > len(messages) else ''
        )
        facts = (
            f'{_plural(channel["agents"], "agent")}, '
            f'{_plural(channel["messages"], "message")}{shown}, '
            f'latest {_ago(channel["last_at"])}'
        )
    here = ''.join(
        f'<li>{_agent_link(a["name"], _state_name(a))}</li>' for a in participants
    )
    who = f'<ul class="who-here" aria-label="Agents in this channel">{here}</ul>'
    names = {a['name'] for a in participants}
    main = f'''{banner}<div class="head"><h1>#{escape(name)}</h1>
<button data-compose>New message</button></div>
<div style="margin:-6px 0 16px"><p class="lede">{facts}</p>{who if here else ''}</div>
<section class="feed">{_feed(messages, False, names)}</section>'''
    compose = _compose(name, draft, [c['name'] for c in channels])
    return _page(
        title, 'channels', main, open_notices, compose, page=f'#{name} · {title}'
    )


def render_channels(
    title: str,
    channels: list[dict[str, Any]],
    previews: dict[str, list[dict[str, Any]]],
    open_notices: int,
    sort: str = 'recent',
    show_previews: bool = True,
) -> str:
    '''Channel list with counts and the latest two messages of each.'''
    sort = sort if sort in CHANNEL_SORTS else 'recent'
    _, key, reverse = CHANNEL_SORTS[sort]
    ordered = sorted(channels, key=key, reverse=reverse)  # type: ignore[arg-type]
    keep = '' if show_previews else '&previews=0'
    sorts = ''.join(
        f'<b aria-current="true">{label}</b>' if k == sort
        else f'<a href="/ui/channels?sort={k}{keep}">{label}</a>'
        for k, (label, _, _) in CHANNEL_SORTS.items()
    )
    flip = '0' if show_previews else '1'
    toggle = (
        f'<a href="/ui/channels?sort={sort}&previews={flip}">'
        f'{"Hide" if show_previews else "Show"} previews</a>'
    )
    rows = []
    for c in ordered:
        preview = ''
        if show_previews:
            lines = ''.join(
                f'<li><b title="{escape(m["agent"])}">{escape(_short(m["agent"]))}</b>'
                f': {escape(_excerpt(m["body"]))}</li>'
                for m in previews.get(c['name'], [])
            )
            preview = f'<ul class="preview">{lines}</ul>'
        rows.append(
            f'<li><div class="title">{_chan_link(c["name"])}<span class="facts">'
            f'{_plural(c["agents"], "agent")}, {_plural(c["messages"], "message")}'
            f'</span>{_ago(c["last_at"])}</div>{preview}</li>'
        )
    body = (
        f'<ul class="chans">{"".join(rows)}</ul>' if rows
        else '<p class="empty">No channels yet. A channel appears with its first '
        'message.</p>'
    )
    main = f'''<div class="head"><h1>Channels</h1><div class="tools">{toggle}</div></div>
<div class="sorts" role="group" aria-label="Sort channels">Sort by {sorts}</div>
<div class="panel">{body}</div>'''
    return _page(title, 'channels', main, open_notices)


def _agent_row(a: dict[str, Any], act: dict[str, Any]) -> str:
    '''One session row on the agents page.'''
    chans = ', '.join(_chan_link(c) for c in act['channels']) or (
        '<span class="sub">none yet</span>'
    )
    status = escape(a['status']) or '<span class="sub">no status</span>'
    home = act['channels'][0] if act['channels'] else ''
    button = (
        '' if a['state'] == 'offline' else
        f'<button class="link" data-compose data-to="{escape(a["name"])}" '
        f'data-channel="{escape(home)}" aria-label="Message {escape(a["name"])}">'
        'Message</button>'
    )
    gone = ' class="gone"' if a['state'] == 'offline' else ''
    short = _short(a['name'])
    prefix = (
        f'<span class="proj">{_name(a["name"][: -len(short)])}</span>'
        if short != a['name'] else ''
    )
    tool = (
        f', <span style="white-space:nowrap">{escape(a["tool"])}</span>'
        if a['tool'] else ''
    )
    return (
        f'<tr id="a-{escape(a["name"])}"{gone}>'
        f'<td class="state">{_state(a["state"])}</td>'
        f'<td class="who wide"><div class="name">{escape(a["host"])}</div>'
        f'<div class="sub id">{prefix}{_name(_short(a["name"]))}{tool}'
        f'</div></td><td class="full status">{status}</td>'
        f'<td class="label full" data-label="In">{chans}</td>'
        f'<td class="num inline" data-label="Messages">{act["messages"]}</td>'
        f'<td class="inline" data-label="Last call">{_ago(a["last_seen"])}</td>'
        f'<td class="act">{button}</td></tr>'
    )


def render_agents(
    title: str,
    agents: list[dict[str, Any]],
    activity: dict[str, dict[str, Any]],
    channels: list[dict[str, Any]],
    open_notices: int,
    everyone: bool = False,
    timeout_min: int = 10,
) -> str:
    '''Agent sessions grouped by project, live first, each with a message button.'''
    rank = {s: i for i, s in enumerate(STATE_ORDER)}
    groups: dict[str, list[dict[str, Any]]] = {}
    for a in sorted(agents, key=lambda a: rank.get(a['state'], 9)):
        groups.setdefault(a['project'], []).append(a)
    idle = {'messages': 0, 'channels': []}
    body = ''.join(
        f'<tbody><tr class="group"><th colspan="7" scope="colgroup">{escape(p)}'
        f'<span>{_plural(len(rows), "session")}</span></th></tr>'
        f'{"".join(_agent_row(a, activity.get(a["name"], idle)) for a in rows)}'
        '</tbody>'
        for p, rows in groups.items()
    )
    toggle = (
        '<a href="/ui/agents">Show the last day only</a>' if everyone
        else '<a href="/ui/agents?all=1">Show all sessions</a>'
    )
    span = 'All sessions on record' if everyone else 'Sessions seen in the last day'
    table = (
        '<table class="agents"><thead><tr><th>State</th><th>Host and session</th>'
        '<th>Status</th>'
        '<th>Channels</th><th class="num">Messages</th><th>Last call</th>'
        f'<th><span class="sr">Actions</span></th></tr></thead>{body}</table>'
        if agents else '<p class="empty">No agent sessions yet. Agents appear here '
        'when they register.</p>'
    )
    meaning = {
        'listening': 'waiting for messages right now',
        'active': f'called in the last {timeout_min} minutes',
        'stale': f'quiet for {timeout_min}+ minutes',
        'offline': 'checked out',
    }
    legend = ''.join(
        f'<li>{_state(s)}<span class="what">: {m}</span></li>'
        for s, m in meaning.items()
    )
    help_ = ''.join(f'<li>{_state(s)}: {m}</li>' for s, m in meaning.items())
    main = f'''<div class="head"><div><h1>Agents</h1>
<p class="lede">{span}, grouped by project, live first.</p></div>
<div class="tools">{toggle}</div></div>
<ul class="legend" aria-label="States">{legend}</ul>
<details class="states"><summary>What the states mean</summary>
<ul class="legend">{help_}</ul></details>
<div class="panel" style="margin-top:12px">{table}</div>'''
    compose = _compose('', None, [c['name'] for c in channels])
    return _page(title, 'agents', main, open_notices, compose)


def render_files(
    title: str,
    files: list[dict[str, Any]],
    used: int,
    capacity: int,
    quota: bool,
    open_notices: int,
    sort: str = 'newest',
) -> str:
    '''File list with a storage bar (``quota``: capacity is the configured cap).'''
    now = datetime.now(UTC)
    sort = sort if sort in FILE_SORTS else 'newest'
    _, key, reverse = FILE_SORTS[sort]
    sorts = ''.join(
        f'<b aria-current="true">{label}</b>' if k == sort
        else f'<a href="/ui/files?sort={k}">{label}</a>'
        for k, (label, _, _) in FILE_SORTS.items()
    )
    rows = []
    for f in sorted(files, key=key, reverse=reverse):  # type: ignore[arg-type]
        left = (datetime.fromisoformat(f['expires_at']) - now).total_seconds()
        soon = left < EXPIRY_SOON
        flag = '<span class="tag">soon</span>' if soon else ''
        rows.append(
            f'<tr><td class="full"><a class="name" href="/api/files/{f["id"]}" '
            f'download>{escape(f["name"])}</a> <span class="sub">file:{f["id"]}</span>'
            f'</td><td class="num inline" data-label="Size">{_size(f["size"])}</td>'
            f'<td class="label full from" data-label="From">'
            f'{_agent_link(f["agent"])}</td>'
            f'<td class="inline" data-label="Uploaded">{_ago(f["created_at"])}</td>'
            f'<td class="inline{" soon" if soon else ""}" data-label="Expires">'
            f'{_ago(f["expires_at"], now)}{flag}</td></tr>'
        )
    table = (
        '<table class="files"><thead><tr><th>Name</th><th class="num">Size</th>'
        '<th>From</th>'
        '<th>Uploaded</th><th>Expires</th></tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table>'
        if files else '<p class="empty">No files stored. Agents share files by '
        'uploading them and posting the <code>file:</code> id.</p>'
    )
    limit = 'quota' if quota else 'available'
    _, pct = _share(used, capacity)
    main = f'''<div class="head"><div><h1>Files</h1>
<p class="lede">Files expiring within a day are marked soon.</p></div></div>
<div class="panel storage"><div class="usage">
<span><b>{_size(used)}</b> used by {_plural(len(files), "file")} ({pct})</span>
<span><b>{_size(max(0, capacity - used))}</b> free of {_size(capacity)} {limit}
</span></div>{_meter(used, capacity)}</div>
<div class="sorts" role="group" aria-label="Sort files">Sort by {sorts}</div>
<div class="panel">{table}</div>'''
    return _page(title, 'files', main, open_notices)
