#!/usr/bin/env python3
"""Curate the output of download-hellodj-stickers.sh into a searchable <=100 MiB HelloDJ pack.

Requirements: Python 3.11+ and Pillow with WebP support.

Examples:
  python -m pip install --user Pillow
  nix-shell -p python312 python312Packages.pillow

Usage:
  ./prepare-hellodj-stickers.py
  ./prepare-hellodj-stickers.py --input ./stickers-upstream --output ./hellodj-stickers
"""
from __future__ import annotations

import argparse, hashlib, io, json, math, re, shutil, sys, unicodedata, zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

try:
    from PIL import Image, ImageSequence, features
except ImportError:
    raise SystemExit("Pillow required: python -m pip install --user Pillow")

MIB = 1024 * 1024
SOURCES = {
    "noto-animated": dict(label="Noto Animated", mb=30, animated=True, license="CC-BY-4.0", home="https://googlefonts.github.io/noto-emoji-animation/", hints=["Noto-Animated"]),
    "fluent-animated": dict(label="Fluent Animated", mb=25, animated=True, license="MIT", home="https://github.com/microsoft/fluentui-emoji-animated", hints=["Fluent-Emoji-Animated"]),
    "kenney": dict(label="Kenney Emotes", mb=15, animated=False, license="CC0-1.0", home="https://kenney.nl/assets/emotes-pack", hints=["Kenney-Emotes-Latest.zip"]),
    "blobmoji": dict(label="Blobmoji", mb=10, animated=False, license="Apache-2.0 / upstream asset licensing", home="https://github.com/C1710/blobmoji", hints=["Blobmoji-Latest.zip"]),
    "fluent-static": dict(label="Fluent Emoji", mb=8, animated=False, license="MIT", home="https://github.com/microsoft/fluentui-emoji", hints=["Fluent-Emoji-Static-Latest.zip"]),
    "openmoji": dict(label="OpenMoji", mb=5, animated=False, license="CC-BY-SA-4.0", home="https://openmoji.org/", hints=["OpenMoji-Latest.zip"]),
}
DEFAULT_ASSET_MB = sum(x["mb"] for x in SOURCES.values())  # 93; leaves 7 MiB headroom
RASTER = {".png", ".gif", ".webp", ".jpg", ".jpeg"}

REACTION = "laugh joy rofl grin smile cry sob sad angry rage scream shock surprised astonished melting thinking confused pleading smirk wink unamused eyebrow side eye rolling eyes salute facepalm shrug yawn sleep tired nervous worried grimacing dizzy woozy vomit sick nerd sunglasses cowboy disguise".split()
CHAOS = "skull dead death devil imp clown poop ghost alien robot ogre fire collision explosion bomb boom warning biohazard radioactive sos anger cursing fight lightning tornado volcano".split()
HANDS = ["thumbs up","thumbs down","clap","raising hands","middle finger","victory","peace","metal","love you gesture","call me","ok hand","folded hands","fist","wave","handshake","heart hands"]
PARTY = "party celebrate celebration confetti balloon fireworks gift birthday cake trophy medal sparkles star 100".split()
LOVE = "heart love kiss hug cupid rose".split()
CREATURE = "cat frog monkey dog unicorn chicken chick penguin owl fox bear panda raccoon otter shark orca whale octopus jellyfish bee butterfly dragon dinosaur sloth".split()
UTILITY = ["warning","stop","check","cross mark","question","exclamation","light bulb","computer","laptop","server","camera","microphone","music","speaker","headphone","controller","rocket","money","trash","wastebasket","hammer","wrench","gear","pencil","books","chart","phone","bell","megaphone","magnifying"]
LOW = ["flag","regional indicator","tram","train","locomotive","passport","customs","baggage","restroom","elevator","office building","hotel","post office","bank","atm","clock face","keycap"]
ALIASES = {
    "laugh": ["lol","lmao","rofl","haha","funny"], "joy": ["lol","lmao","haha","laugh"],
    "skull": ["dead","ded","rip"], "raised eyebrow": ["sus","suspicious","doubt","hmm","side eye"],
    "side eye": ["sus","suspicious","doubt"], "eyes": ["look","watch","watching"],
    "thinking": ["hmm","think","wonder","sus"], "fire": ["lit","hot","flame"],
    "middle finger": ["flip off","fuck","angry"], "poop": ["shit","crap"],
    "cry": ["sad","tears","sob"], "party": ["celebrate","yay"], "heart": ["love","romance"],
    "thumbs up": ["yes","approve","good","ok"], "thumbs down": ["no","disapprove","bad"],
    "warning": ["alert","caution","danger"], "check": ["yes","done","correct"],
    "cross mark": ["no","wrong","x"], "bomb": ["boom","explode","explosion"],
    "rocket": ["launch","ship"], "vomit": ["puke","sick"], "rage": ["mad","angry","furious"],
    "clown": ["clowning","idiot"], "shrug": ["idk","whatever","dunno"], "100": ["hundred","perfect"]
}

@dataclass
class Candidate:
    source: str; path: str; name: str; emoji: str | None; tags: set[str]; category: str
    score: float; expected_animated: bool; semantic: str; opener: object; input_bytes: int

@dataclass
class Record:
    id: str; file: str; name: str; emoji: str | None; tags: list[str]; category: str
    source: str; animated: bool; license: str; bytes: int; sha256: str


def words(s: str) -> str:
    s = re.sub(r"[_-]+", " ", s)
    s = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def slug(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii","ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "sticker"


def from_hex(stem: str):
    stem = stem.lower().replace("emoji_u", "").replace("emoji-", "")
    if not re.fullmatch(r"[0-9a-f]{4,8}(?:[_-][0-9a-f]{4,8})*", stem): return None, None
    try: cps = [int(x,16) for x in re.split(r"[_-]",stem)]
    except ValueError: return None, None
    emo = "".join(chr(x) for x in cps); names=[]
    for cp in cps:
        try: n=unicodedata.name(chr(cp))
        except ValueError: continue
        if "FITZPATRICK" in n or "VARIATION SELECTOR" in n or n=="ZERO WIDTH JOINER": continue
        names.append(n)
    return emo, " ".join(names).title() if names else None


def asset_name(path: str, source: str):
    p=PurePosixPath(path); emo, n=from_hex(p.stem)
    if n: return n, emo
    opts=[words(p.stem)] + [words(x) for x in reversed(p.parts[:-1])]
    noise={"3d","color","colour","flat","high contrast","outline","filled","default","static","png","webp","assets","asset"}
    name=next((x for x in opts if len(x)>2 and x.lower() not in noise and not re.fullmatch(r"\d+x\d+",x)), words(p.stem))
    name=re.sub(r"\b(3d|color|colour|flat|high contrast|outline|filled|default|static)\b","",name,flags=re.I)
    return re.sub(r"\s+"," ",name).strip().title(), emo


def contains(text, seq): return any(x in text for x in seq)

def category(name, source):
    t=name.lower()
    if source=="blobmoji": return "blobs"
    if contains(t,CHAOS): return "chaos"
    if contains(t,HANDS): return "hands"
    if contains(t,LOVE): return "love"
    if contains(t,PARTY): return "party"
    if contains(t,CREATURE): return "creatures"
    if contains(t,UTILITY): return "symbols"
    if contains(t,REACTION): return "reactions"
    return "stuff"


def tags_for(name, emoji, source):
    t=name.lower(); out=set(re.findall(r"[a-z0-9]+",t)); out.add(source.replace("-"," "))
    for phrase in HANDS+UTILITY:
        if phrase in t: out.add(phrase)
    for trigger, extra in ALIASES.items():
        if trigger in t: out.update(extra)
    if emoji: out.add(emoji)
    return {x for x in out if x and len(x)<50}


def score(name,path,source,emoji):
    t=(name+" "+words(path)).lower(); s={"noto-animated":20,"fluent-animated":18,"kenney":14,"blobmoji":16,"fluent-static":8,"openmoji":4}[source]
    for seq,w in ((REACTION,12),(CHAOS,14),(HANDS,12),(PARTY,10),(LOVE,9),(CREATURE,7),(UTILITY,6),(LOW,-25)):
        s += sum(w for x in seq if x in t)
    p=path.lower()
    if source=="openmoji":
        if "color" in p: s+=18
        if "618" in p: s+=12
        if "black" in p: s-=50
    if source=="fluent-static":
        if "/3d/" in p: s+=18
        elif "/color/" in p: s+=10
        if "high_contrast" in p or "high contrast" in p: s-=35
    if source=="kenney" and any(x in p for x in ("/black/","/white/","outline")): s-=20
    if re.search(r"1f3f[b-f]",p): s-=100
    if "flag" in t or "regional indicator" in t: s-=120
    return s


def make_candidate(source,path,opener,size):
    if PurePosixPath(path).suffix.lower() not in RASTER: return None
    low=path.lower()
    if any(x in low for x in ("/__macosx","/node_modules/","/docs/","/test/","/tests/","/preview/")): return None
    if any(x in PurePosixPath(low).name for x in ("logo","license","readme")): return None
    n,e=asset_name(path,source); c=category(n,source); sem=f"{source}:{re.sub(r'[^a-z0-9]+',' ',n.lower()).strip()}"
    return Candidate(source,path,n,e,tags_for(n,e,source),c,score(n,path,source,e),SOURCES[source]["animated"],sem,opener,size)


def source_path(root,cfg):
    for h in cfg["hints"]:
        p=root/h
        if p.exists(): return p
    return None


def scan(root,source):
    p=source_path(root,SOURCES[source]); out=[]
    if not p:
        print(f"WARNING missing {SOURCES[source]['label']}",file=sys.stderr); return out
    print(f"Scanning {SOURCES[source]['label']}: {p}")
    if p.is_dir():
        for f in p.rglob("*"):
            if not f.is_file() or f.suffix.lower() not in RASTER: continue
            rel=f.relative_to(p).as_posix()
            c=make_candidate(source,rel,lambda f=f:f.open("rb"),f.stat().st_size)
            if c: out.append(c)
    elif zipfile.is_zipfile(p):
        zf=zipfile.ZipFile(p)
        for i in zf.infolist():
            if i.is_dir() or PurePosixPath(i.filename).suffix.lower() not in RASTER: continue
            c=make_candidate(source,i.filename,lambda i=i,zf=zf:io.BytesIO(zf.read(i)),i.file_size)
            if c: out.append(c)
        for c in out: c._zip=zf
    # Keep only the best style/variant per semantic name within each source.
    best={}
    for c in sorted(out,key=lambda x:(-x.score,x.input_bytes,x.path)):
        best.setdefault(c.semantic,c)
    return sorted(best.values(),key=lambda x:(-x.score,x.input_bytes,x.name.lower()))


def fit_frame(im,max_px):
    f=im.convert("RGBA"); f.thumbnail((max_px,max_px),Image.Resampling.LANCZOS); return f


def convert(c,out,max_px,fps,max_frames,quality):
    with c.opener() as fh:
        im=Image.open(fh); animated=bool(getattr(im,"is_animated",False) and getattr(im,"n_frames",1)>1)
        tmp=out.with_suffix(".tmp.webp"); out.parent.mkdir(parents=True,exist_ok=True)
        if animated:
            min_ms=max(1,round(1000/fps)); raw=getattr(im,"n_frames",1); stride=max(1,math.ceil(raw/max_frames)); frames=[]; durations=[]; pending=0
            for idx,fr in enumerate(ImageSequence.Iterator(im)):
                pending += max(10,min(int(fr.info.get("duration",im.info.get("duration",50)) or 50),2000))
                if idx%stride and idx!=raw-1: continue
                if pending<min_ms and idx!=raw-1: continue
                frames.append(fit_frame(fr,max_px)); durations.append(max(min_ms,pending)); pending=0
                if len(frames)>=max_frames: break
            if not frames: frames=[fit_frame(im,max_px)]; durations=[100]
            frames[0].save(tmp,"WEBP",save_all=True,append_images=frames[1:],duration=durations,loop=int(im.info.get("loop",0) or 0),lossless=False,quality=quality,method=4,minimize_size=True)
            for f in frames: f.close()
        else:
            im.seek(0); f=fit_frame(im,max_px); f.save(tmp,"WEBP",lossless=True,method=6); f.close()
    data=tmp.read_bytes(); digest=hashlib.sha256(data).hexdigest(); tmp.replace(out)
    return animated,len(data),digest


def build_zips(root,records):
    zdir=root/"legacy-zips"; shutil.rmtree(zdir,ignore_errors=True); zdir.mkdir()
    grouped=defaultdict(list)
    for r in records: grouped[r.category].append(r)
    for cat,items in grouped.items():
        with zipfile.ZipFile(zdir/f"Stickers - {cat.title()}.zip","w",compression=zipfile.ZIP_STORED) as z:
            for r in items: z.write(root/r.file,arcname=Path(r.file).name)


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--input",type=Path,default=Path("./stickers-upstream")); ap.add_argument("--output",type=Path,default=Path("./hellodj-stickers"))
    ap.add_argument("--budget-mb",type=float,default=100); ap.add_argument("--asset-budget-mb",type=float)
    ap.add_argument("--max-px",type=int,default=256); ap.add_argument("--max-total",type=int,default=1200); ap.add_argument("--max-animated",type=int,default=275)
    ap.add_argument("--max-animation-fps",type=int,default=20); ap.add_argument("--max-animation-frames",type=int,default=120); ap.add_argument("--animated-quality",type=int,default=72)
    args=ap.parse_args()
    if not features.check("webp"): raise SystemExit("Pillow has no WebP support")
    root=args.input.resolve(); out=args.output.resolve()
    if not root.is_dir(): raise SystemExit(f"Input missing: {root}. Run download-hellodj-stickers.sh first.")
    shutil.rmtree(out,ignore_errors=True); (out/"assets").mkdir(parents=True)
    package_budget=int(args.budget_mb*MIB); asset_budget=int((args.asset_budget_mb if args.asset_budget_mb is not None else args.budget_mb-7)*MIB)
    print(f"Package target {args.budget_mb:.1f} MiB; image budget {asset_budget/MIB:.1f} MiB")

    bysrc={s:scan(root,s) for s in SOURCES}
    scale=asset_budget/(DEFAULT_ASSET_MB*MIB); quotas={s:int(cfg["mb"]*MIB*scale) for s,cfg in SOURCES.items()}
    records=[]; used_ids=set(); hashes=set(); consumed=set(); source_bytes=Counter(); asset_bytes=0; animated_count=0

    def add(c,limit=None):
        nonlocal asset_bytes,animated_count
        key=(c.source,c.path)
        if key in consumed or len(records)>=args.max_total or c.score<0: return False
        consumed.add(key)
        if c.expected_animated and animated_count>=args.max_animated: return False
        base=f"{slug(c.source)}-{slug(c.name)}"; ident=base; n=2
        while ident in used_ids: ident=f"{base}-{n}"; n+=1
        used_ids.add(ident); rel=Path("assets")/c.category/f"{ident}.webp"; dest=out/rel
        try: animated,size,digest=convert(c,dest,args.max_px,args.max_animation_fps,args.max_animation_frames,args.animated_quality)
        except Exception as e:
            dest.unlink(missing_ok=True); print(f"SKIP {c.source}:{c.path}: {e}",file=sys.stderr); return False
        if asset_bytes+size>asset_budget or (limit is not None and source_bytes[c.source]+size>limit) or digest in hashes or (animated and animated_count>=args.max_animated):
            dest.unlink(missing_ok=True); return False
        hashes.add(digest); asset_bytes+=size; source_bytes[c.source]+=size; animated_count+=int(animated)
        records.append(Record(ident,rel.as_posix(),c.name,c.emoji,sorted(c.tags),c.category,c.source,animated,SOURCES[c.source]["license"],size,digest))
        if len(records)%25==0: print(f"  {len(records)} stickers, {animated_count} animated, {asset_bytes/MIB:.2f} MiB")
        return True

    for s,cands in bysrc.items():
        print(f"\n[{SOURCES[s]['label']}] quota {quotas[s]/MIB:.1f} MiB")
        for c in cands:
            if source_bytes[s]>=quotas[s] or asset_bytes>=asset_budget or len(records)>=args.max_total: break
            add(c,quotas[s])
        print(f"  -> {sum(r.source==s for r in records)} stickers / {source_bytes[s]/MIB:.2f} MiB")

    leftovers=[c for arr in bysrc.values() for c in arr if (c.source,c.path) not in consumed and c.score>=0]
    leftovers.sort(key=lambda x:(-x.score,x.input_bytes,x.name.lower()))
    print("\nRedistributing unused budget...")
    for c in leftovers:
        if asset_bytes>=asset_budget or len(records)>=args.max_total: break
        add(c,None)

    # Manifest for future searchable catalog.
    cat_counts=Counter(r.category for r in records); src_counts=Counter(r.source for r in records)
    manifest={
        "schema_version":1,"generated_at":datetime.now(timezone.utc).isoformat(),"package_budget_bytes":package_budget,
        "asset_budget_bytes":asset_budget,"asset_bytes":asset_bytes,"sticker_count":len(records),"animated_count":animated_count,
        "categories":[{"slug":c,"name":c.title(),"count":n} for c,n in sorted(cat_counts.items())],
        "sources":[{"slug":s,"name":SOURCES[s]["label"],"license":SOURCES[s]["license"],"homepage":SOURCES[s]["home"],"count":src_counts[s],"bytes":source_bytes[s]} for s in SOURCES if src_counts[s]],
        "stickers":[{"id":r.id,"file":r.file,"name":r.name,"emoji":r.emoji,"tags":r.tags,"search_text":" ".join([r.name.lower(),*map(str.lower,r.tags),r.category,r.source]),"category":r.category,"source":r.source,"animated":r.animated,"license":r.license,"bytes":r.bytes,"sha256":r.sha256} for r in sorted(records,key=lambda x:(x.category,x.name.lower()))]
    }
    (out/"manifest.json").write_text(json.dumps(manifest,indent=2,ensure_ascii=False)+"\n",encoding="utf-8")
    licenses=["# HelloDJ Curated Sticker Pack — Third-Party Assets",""]
    for s,cfg in SOURCES.items(): licenses += [f"## {cfg['label']}",f"- Source: {cfg['home']}",f"- License: {cfg['license']}",""]
    (out/"THIRD_PARTY_ASSETS.md").write_text("\n".join(licenses),encoding="utf-8")
    build_zips(out,records)
    (out/"DOCKERFILE.snippet").write_text("# Curated sticker assets\nCOPY stickers/ /app/stickers/\n",encoding="utf-8")
    report=["HelloDJ curated sticker build","="*40,f"Stickers: {len(records)}",f"Animated: {animated_count}",f"Static: {len(records)-animated_count}",f"Assets: {asset_bytes/MIB:.2f} MiB",""]
    report += [f"{SOURCES[s]['label']:<20} {src_counts[s]:4d}  {source_bytes[s]/MIB:7.2f} MiB" for s in SOURCES if src_counts[s]]
    report += ["","Categories:"]+[f"{c:<16} {n:4d}" for c,n in sorted(cat_counts.items())]
    text="\n".join(report)+"\n"; (out/"BUILD_REPORT.txt").write_text(text,encoding="utf-8"); print("\n"+text)
    print(f"Ready: {out}")
    print(f"Manifest: {out/'manifest.json'}")
    print(f"Current-catalog ZIPs: {out/'legacy-zips'}")

if __name__=="__main__": main()
