"""The viewer's stylesheet. System fonts only: nothing is fetched from anywhere.

Design rules, so later changes keep the same character:

* Colour means change. The page itself is ink on paper; the only saturated colours are green (added),
  blue (changed), red (deleted) and signal amber (changed outside the store's own history). Shape says it
  too, so nothing depends on colour alone.
* The timeline is a chain. Every entry hangs on one spine; what happened decides the shape of its node.
* Two type roles. Interface text is a humanist sans close to Open Sans (Segoe UI on Windows; Open Sans or Noto
  Sans if installed, otherwise the system font). The agent's own words, which are usually markdown, are set in
  the monospace face of a code editor, the same face used for ids and hashes, so they look like the file they
  came from. Only fonts already on the computer are used.
* No cards, pills or shadows. Structure comes from rules, spacing and the chain.
"""

_DARK = """  --paper:#0d1a23;--sheet:#12232e;--ink:#dce6eb;--muted:#9bb0bc;--faint:#7c919d;--rule:#2a4251;--wash:#193040;
  --add:#58c995;--add-wash:#133a2a;--chg:#86abff;--chg-wash:#1a2d54;--del:#ff8f7a;--del-wash:#4a211b;
  --out:#f2b632;--out-ink:#f6d98b;--out-wash:#3d2f0f;--focus:#86abff;--on:#0d1a23;--on-out:#2b1c00;
"""

CSS = """
:root{
  color-scheme:light dark;
  --paper:#edf0f2;--sheet:#f8f9fa;--ink:#10283a;--muted:#52687a;--faint:#5a6e7e;--rule:#c2cdd4;--wash:#e0e7ec;
  --add:#17704a;--add-wash:#d8eee2;--chg:#2453b0;--chg-wash:#dce6f7;--del:#b0301d;--del-wash:#f7dfda;
  --out:#e9a800;--out-ink:#5a3b00;--out-wash:#fbedc2;--focus:#2453b0;--on:#fff;--on-out:#3a2600;
  --page:1480px;--seq:52px;--rail:46px;--node:28px;--gutter:clamp(16px,3.2vw,56px);
  --ui:"Open Sans","Segoe UI Variable Text","Segoe UI","Noto Sans",Roboto,system-ui,"Helvetica Neue",Arial,sans-serif;
  --mono:"Cascadia Mono","Cascadia Code",Consolas,"SF Mono",Menlo,ui-monospace,"DejaVu Sans Mono",monospace;
}
@media (prefers-color-scheme:dark){:root:not(.theme-light){@@DARK@@}}
:root.theme-dark{color-scheme:dark;@@DARK@@}
:root.theme-light{color-scheme:light}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--paper);color:var(--ink);font:15px/1.5 var(--ui)}
a{color:var(--ink);text-decoration-thickness:1px;text-underline-offset:3px}
a:hover{text-decoration-thickness:2px}
:focus-visible{outline:3px solid var(--focus);outline-offset:2px}
code,pre,.mono{font-family:var(--mono);font-size:13px}
h1,h2,h3{font-family:var(--ui);margin:0;line-height:1.2}
h1{font-size:30px;font-weight:600;letter-spacing:-.012em;max-width:26ch}
h2{font-size:18px;font-weight:600;margin:40px 0 12px}
h3{font-size:15px;font-weight:600;margin:22px 0 6px}
p{margin:0 0 10px}
small{font-size:13px;color:var(--muted)}
.sub{color:var(--muted);margin:10px 0 28px;max-width:62ch}

/* masthead */
header.top{max-width:var(--page);margin:0 auto;padding:22px var(--gutter) 0;display:flex;flex-wrap:wrap;align-items:baseline;gap:4px 26px}
.brand{position:relative;display:inline-block;padding-left:34px;font-size:21px;font-weight:600;letter-spacing:-.01em}
.brand::before,.brand::after{content:"";position:absolute;top:50%;width:17px;height:10px;margin-top:-4px;border:2px solid var(--ink);border-radius:6px}
.brand::before{left:0}.brand::after{left:9px}
.where{font:12.5px var(--mono);color:var(--muted)}
.ro{font-size:13px;color:var(--muted)}
nav.tabs{margin-left:auto;display:flex;flex-wrap:wrap;gap:0 24px}
nav.tabs a{padding:10px 0 8px;color:var(--muted);text-decoration:none;border-bottom:2px solid transparent}
nav.tabs a:hover{color:var(--ink);border-bottom-color:var(--rule)}
nav.tabs a[aria-current=page]{color:var(--ink);font-weight:600;border-bottom-color:var(--ink)}
main{max-width:var(--page);margin:0 auto;padding:30px var(--gutter) 60px}
footer{max-width:var(--page);margin:0 auto;padding:0 var(--gutter) 40px;color:var(--muted);font-size:13px}
footer::before{content:"";display:block;border-top:1px solid var(--rule);margin-bottom:18px}

/* colour theme switch: three links, the current one filled */
.theme{display:flex;align-self:center;margin-left:6px;border:1px solid var(--rule);border-radius:4px;overflow:hidden}
.theme a{padding:5px 12px;font-size:13px;line-height:1.4;color:var(--muted);text-decoration:none}
.theme a:hover{background:var(--wash);color:var(--ink)}
.theme a[aria-current=true]{background:var(--ink);color:var(--paper);font-weight:600}
@media (max-width:860px){.theme{margin-left:0}}

/* filters */
.filters{display:flex;flex-wrap:wrap;gap:0 22px;margin:0 0 26px}
.filters a{display:inline-flex;align-items:center;min-height:40px;color:var(--muted);text-decoration:none;border-bottom:2px solid transparent}
.filters a:hover{color:var(--ink);border-bottom-color:var(--rule)}
.filters a[aria-current=page]{color:var(--ink);font-weight:600;border-bottom-color:var(--ink)}

/* timeline layout: a list on the left, a wide reading pane on the right */
.layout{display:grid;grid-template-columns:minmax(380px,500px) minmax(0,1fr);gap:clamp(28px,3vw,48px);align-items:start}
.sheet{position:sticky;top:16px;max-height:calc(100vh - 32px);overflow:auto;background:var(--sheet);border-top:3px solid var(--ink);padding:20px 26px 26px}
.sheet-main{min-width:0}
.sheet-side{margin-top:26px;padding-top:4px;border-top:1px solid var(--rule)}
.sheet-side h3{margin-top:14px}
@media (max-width:1100px){.layout{grid-template-columns:minmax(0,1fr);gap:28px}.layout.has-sheet .sheet{order:-1}.sheet{position:static;max-height:78vh}}
@media (max-width:860px){h1{font-size:25px}nav.tabs{margin-left:0;width:100%}}
@media (max-width:640px){:root{--seq:38px;--rail:32px;--node:26px}}

/* the chain: one round dot per entry. Its colour and icon say what happened. */
.chain{list-style:none;margin:0;padding:0;max-width:980px}
.chain>li{position:relative;display:grid;grid-template-columns:var(--seq) var(--rail) minmax(0,1fr);
  background:linear-gradient(var(--rule),var(--rule)) calc(var(--seq) + var(--rail)/2 - 1px) 0/2px 100% no-repeat}
.chain>li:first-child{background-position:calc(var(--seq) + var(--rail)/2 - 1px) 25px;background-size:2px calc(100% - 25px)}
.chain>li:last-child{background-size:2px 25px}
.chain.goes-on>li:last-child{background-size:2px 100%}
.chain>li:first-child:last-child{background:none}
.chain>li::before{content:"";position:absolute;left:calc(var(--seq) + var(--rail)/2 - var(--node)/2);top:11px;width:var(--node);height:var(--node);
  border-radius:50%;background-color:var(--disc,var(--ink));background-repeat:no-repeat;transition:none}
.seq{grid-column:1;justify-self:end;padding-top:14px;font:12px var(--mono);color:var(--faint)}
.chain>li>a.row{grid-column:3}
.n-add{--disc:var(--add)}.n-update{--disc:var(--chg)}.n-delete{--disc:var(--del)}
.n-external{--disc:var(--out);--on:var(--on-out)}
.n-snapshot,.n-rollback{--disc:var(--ink);--on:var(--paper)}.n-snap-del{--disc:var(--faint);--on:var(--paper)}
/* icons, drawn with gradients so they need no image files */
.chain>li.n-add::before{background-image:linear-gradient(var(--on),var(--on)),linear-gradient(var(--on),var(--on));
  background-position:8px 13px,13px 8px;background-size:12px 2px,2px 12px}
.chain>li.n-delete::before,.chain>li.n-snap-del::before{
  background-image:linear-gradient(var(--disc),var(--disc)),linear-gradient(var(--disc),var(--disc)),linear-gradient(var(--on),var(--on)),linear-gradient(var(--on),var(--on)),linear-gradient(var(--on),var(--on));
  background-position:11px 14px,15px 14px,9px 12px,7px 9px,11px 7px;background-size:2px 5px,2px 5px,10px 9px,14px 2px,6px 2px}
.chain>li.n-snapshot::before{
  background-image:radial-gradient(circle at 14px 15.5px,var(--disc) 0 3.4px,transparent 4px),linear-gradient(var(--on),var(--on)),linear-gradient(var(--on),var(--on));
  background-position:0 0,6px 10px,11px 7px;background-size:100% 100%,16px 11px,6px 3px}
.chain>li.n-rollback::before{background-image:linear-gradient(var(--on),var(--on));background-position:9px 13px;background-size:11px 2px}
.chain>li.n-rollback::after{content:"";position:absolute;width:8px;height:8px;left:calc(var(--seq) + var(--rail)/2 - 6px);
  top:calc(11px + var(--node)/2 - 4px);transform:rotate(45deg);border-left:2px solid var(--on);border-bottom:2px solid var(--on)}
.chain>li.n-external::before{background-image:linear-gradient(var(--on),var(--on)),linear-gradient(var(--on),var(--on));
  background-position:13px 7px,13px 19px;background-size:2px 10px,2px 2px}
.chain>li.n-update::after{content:"";position:absolute;width:5px;height:15px;left:calc(var(--seq) + var(--rail)/2 - 2.5px);top:calc(11px + var(--node)/2 - 7.5px);
  transform:rotate(45deg);background:linear-gradient(var(--on) 0 18%,transparent 18% 27%,var(--on) 27%);clip-path:polygon(0 0,100% 0,100% 78%,50% 100%,0 78%)}
.chain>li:has(a[aria-current=true])::before{box-shadow:0 0 0 3px var(--paper),0 0 0 5px var(--ink)}

a.row{display:block;position:relative;padding:10px 14px 12px;margin:0 0 6px;color:var(--ink);text-decoration:none}
a.row:hover{background:var(--wash)}
a.row[aria-current=true]{background:var(--wash);box-shadow:inset 3px 0 0 var(--ink)}
a.row:hover .what{text-decoration:underline}
.top{display:flex;flex-wrap:wrap;align-items:center;gap:4px 10px}
.what{font-size:15.5px;font-weight:600;overflow-wrap:anywhere}
.when{margin-left:auto;font-size:12px;color:var(--muted);white-space:nowrap}
.mem{margin:5px 0 0;font:13.5px/1.6 var(--mono);overflow-wrap:anywhere;display:-webkit-box;-webkit-line-clamp:4;-webkit-box-orient:vertical;overflow:hidden}
.n-delete .mem{color:var(--muted);text-decoration:line-through;text-decoration-thickness:1px}
.meta{margin-top:4px;font-size:13px;color:var(--muted);display:flex;flex-wrap:wrap;gap:0 14px}
.flag{color:var(--out-ink);background:var(--out-wash);padding:0 6px;box-shadow:inset 0 -2px 0 var(--out)}
.n-external>a.row{background:var(--out-wash);box-shadow:inset 4px 0 0 var(--out)}
.n-external>a.row[aria-current=true]{box-shadow:inset 4px 0 0 var(--out-ink)}
.label{font-size:15px}

/* status badges */
.badge{display:inline-block;padding:1px 8px;border-radius:4px;font-size:12px;font-weight:600;letter-spacing:.04em;line-height:1.5;white-space:nowrap}
.op-add{background:var(--add-wash);color:var(--add)}
.op-update{background:var(--chg-wash);color:var(--chg)}
.op-delete{background:var(--del-wash);color:var(--del)}
.op-external{background:var(--out-wash);color:var(--out-ink);box-shadow:inset 0 0 0 1px var(--out)}
.op-rollback{background:var(--ink);color:var(--paper)}
.op-snapshot,.op-snap-del{background:var(--wash);color:var(--ink);box-shadow:inset 0 0 0 1px var(--rule)}

/* the open entry */
ul.hints{list-style:none;margin:6px 0 12px;padding:0}
ul.hints li{padding:6px 0;border-bottom:1px solid var(--rule);overflow-wrap:anywhere}
ul.hints code{font-family:var(--mono);font-size:13px;color:var(--muted)}
.flag.hint{background:var(--wash);box-shadow:inset 0 -2px 0 var(--rule);color:var(--ink)}
ul.files{list-style:none;margin:6px 0 14px;padding:0;font:14px/1.8 var(--mono)}
.what-did{display:inline-block;min-width:8ch;color:var(--muted)}
ul.files small{color:var(--muted);font-family:var(--ui)}
.sheet h2{margin:0 0 6px;font-size:24px;overflow-wrap:anywhere}
.sheet .where2{margin:0 0 14px;font:12.5px var(--mono);color:var(--muted)}
.verdict{font-weight:600;margin:0 0 6px}
.why{color:var(--muted);margin:0 0 4px}
.sheet h3{margin-top:22px}
.redline,.quote{font:14px/1.75 var(--mono);white-space:pre-wrap;overflow-wrap:anywhere;margin:6px 0 4px}
.ins{background:var(--chg-wash);color:var(--chg);text-decoration:underline;text-decoration-thickness:2px;text-underline-offset:3px;padding:0 1px}
.rm + .ins{margin-left:.5ch}
.rm{background:var(--del-wash);color:var(--del);text-decoration:line-through;text-decoration-thickness:2px;padding:0 1px}
.redline .ln{min-height:1.75em}
.ln.gone,.ln.added{margin:3px 0;padding:2px 10px 2px calc(10px + 2ch);text-indent:-2ch;border-left:3px solid}
.ln.gone{border-left-color:var(--del);background:var(--del-wash)}
.ln.added{border-left-color:var(--chg);background:var(--chg-wash)}
.ln.gone::before{content:"\\2212\\00a0";color:var(--del)}
.ln.added::before{content:"+\\00a0";color:var(--chg)}
.ln.gone .rm,.ln.added .ins{background:none;text-decoration:none;padding:0}
.ln.gone .rm{color:var(--del)}.ln.added .ins{color:var(--chg)}
.fold{margin:8px 0;padding:3px 0;font:13px var(--ui);color:var(--muted);text-align:center;border-top:1px dashed var(--rule);border-bottom:1px dashed var(--rule)}
.quote.gone{color:var(--muted);text-decoration:line-through;text-decoration-thickness:1px}
.change-key{margin:0 0 8px;font-size:13px;color:var(--muted)}
.change-key .ins,.change-key .rm{padding:0 4px}
dl.facts{display:grid;grid-template-columns:96px minmax(0,1fr);gap:7px 14px;margin:0;font-size:14px}
dl.facts dt{color:var(--muted)}dl.facts dd{margin:0;overflow-wrap:anywhere}
details{margin:12px 0 0}
summary{cursor:pointer;color:var(--muted)}summary:hover{color:var(--ink)}
pre{margin:6px 0;padding:10px 12px;background:var(--wash);white-space:pre-wrap;overflow-wrap:anywhere;max-height:420px;overflow:auto;font:13px/1.5 var(--mono)}
pre .add{color:var(--add);display:block}pre .del{color:var(--del);display:block}pre .hunk{color:var(--muted);display:block}

/* overview */
.headline{max-width:24ch}
.fingerprint{display:block;margin:4px 0 8px;padding:10px 12px;background:var(--sheet);border-left:3px solid var(--ink);overflow-wrap:anywhere}
.attention{margin-top:34px;padding-top:2px}
.putback{margin:30px 0 0;padding:2px 0 2px 16px;border-left:4px solid var(--out)}.putback h2{margin-top:0}.putback pre{margin:10px 0}
.attention h2{margin-top:0}

/* tables */
.table-wrap{overflow-x:auto}
table{border-collapse:collapse;width:100%}
th,td{text-align:left;padding:10px 14px 10px 0;border-bottom:1px solid var(--rule);vertical-align:top;overflow-wrap:anywhere}
th{font-weight:600;font-size:13px;color:var(--muted);border-bottom:2px solid var(--ink)}
td:first-child,th:first-child{padding-left:0}
tbody tr:hover{background:var(--wash)}
td .mem{margin:0;font-size:13.5px}

/* compare */
form.pick{display:flex;flex-wrap:wrap;gap:14px 22px;align-items:end;margin:0 0 30px}
form.pick label{display:flex;flex-direction:column;gap:5px;color:var(--muted);font-size:13px}
select{min-height:42px;padding:0 10px;border:0;border-bottom:2px solid var(--ink);border-radius:0;background:var(--sheet);color:var(--ink);font:15px var(--ui)}
button{min-height:42px;padding:0 20px;border:0;border-radius:2px;background:var(--ink);color:var(--paper);font:600 15px var(--ui);cursor:pointer}
button:hover{background:var(--chg)}
input[type=checkbox]{width:18px;height:18px;margin-right:6px;accent-color:var(--ink);vertical-align:-3px}
.changes{display:flex;flex-direction:column;max-width:1100px}
.change{display:grid;grid-template-columns:minmax(150px,200px) minmax(0,1fr);gap:4px 28px;padding:16px 0;border-top:1px solid var(--rule)}
.change:first-child{border-top:2px solid var(--ink)}
.change .who{display:flex;flex-direction:column;align-items:flex-start;gap:5px}
@media (max-width:640px){.change{grid-template-columns:minmax(0,1fr)}}

/* notices, pager */
.notice{max-width:980px;margin:16px 0;padding:11px 16px;border-left:5px solid var(--out);background:var(--out-wash);color:var(--out-ink)}
.notice.ok{border-left-color:var(--add);background:var(--add-wash);color:var(--add)}
.notice.bad{border-left-color:var(--del);background:var(--del-wash);color:var(--del)}
.pager{display:flex;gap:26px;margin:20px 0 0 calc(var(--seq) + var(--rail) + 14px)}
.pager a{min-height:40px;display:inline-flex;align-items:center}
.state{font-size:19px;font-weight:600;margin:0 0 14px;max-width:44ch}
.state.ok{color:var(--add)}.state.bad{color:var(--del)}
ul.problems{padding-left:20px}
"""
CSS = CSS.replace("@@DARK@@", _DARK.strip())
