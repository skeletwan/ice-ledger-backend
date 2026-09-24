"""Hockey checklist: flagship sets + learned rows from saved cards."""
import re
import sqlite3
import time

SETS = [
    # year, brand, set, insert, parallels (name, print hint)
    ("2025-26", "Upper Deck", "Series 1", "Young Guns", ["Base", "Clear Cut", "Exclusives /100", "High Gloss /10", "Deluxe /250"]),
    ("2025-26", "Upper Deck", "Series 1", "", ["Base", "Canvas", "UD Exclusives /100", "High Gloss /10"]),
    ("2025-26", "Upper Deck", "Series 2", "Young Guns", ["Base", "Clear Cut", "Exclusives /100", "High Gloss /10", "Deluxe /250"]),
    ("2024-25", "Upper Deck", "Series 1", "Young Guns", ["Base", "Clear Cut", "Exclusives /100", "High Gloss /10", "Deluxe /250"]),
    ("2024-25", "Upper Deck", "Series 1", "", ["Base", "Canvas", "UD Exclusives /100", "High Gloss /10"]),
    ("2024-25", "Upper Deck", "Series 2", "Young Guns", ["Base", "Clear Cut", "Exclusives /100", "High Gloss /10", "Deluxe /250"]),
    ("2023-24", "Upper Deck", "Series 1", "Young Guns", ["Base", "Clear Cut", "Exclusives /100", "High Gloss /10", "Deluxe /250"]),
    ("2023-24", "Upper Deck", "Series 2", "Young Guns", ["Base", "Clear Cut", "Exclusives /100", "High Gloss /10"]),
    ("2022-23", "Upper Deck", "Series 1", "Young Guns", ["Base", "Clear Cut", "Exclusives /100", "High Gloss /10"]),
    ("2021-22", "Upper Deck", "Series 1", "Young Guns", ["Base", "Clear Cut", "Exclusives /100", "High Gloss /10"]),
    ("2020-21", "Upper Deck", "Series 1", "Young Guns", ["Base", "Clear Cut", "Exclusives /100", "High Gloss /10"]),
    ("2019-20", "Upper Deck", "Series 1", "Young Guns", ["Base", "Clear Cut", "Exclusives /100", "High Gloss /10"]),
    ("2016-17", "Upper Deck", "Series 1", "Young Guns", ["Base", "Exclusives /100", "High Gloss /10"]),
    ("2015-16", "Upper Deck", "Series 1", "Young Guns", ["Base", "Exclusives /100", "High Gloss /10"]),
    ("2005-06", "Upper Deck", "Series 1", "Young Guns", ["Base"]),
    ("2024-25", "Upper Deck", "Extended", "Young Guns", ["Base", "Clear Cut", "Exclusives /100"]),
    ("2024-25", "Upper Deck", "SP Authentic", "Future Watch", ["Base /999", "Inscribed", "Gold /150", "Patch"]),
    ("2023-24", "Upper Deck", "SP Authentic", "Future Watch", ["Base /999", "Inscribed", "Gold /150"]),
    ("2024-25", "Upper Deck", "The Cup", "", ["Base /249", "Gold /24", "Black /8", "Printing Plate 1/1"]),
    ("2023-24", "Upper Deck", "The Cup", "", ["Base /249", "Gold /24", "Black /8"]),
    ("2024-25", "Upper Deck", "Artifacts", "", ["Base", "Ruby /499", "Sapphire /85", "Emerald /99", "Gold /65", "Auto"]),
    ("2024-25", "Upper Deck", "Synergy", "", ["Base", "Red", "Purple /899", "Gold /65", "Black /5"]),
    ("2024-25", "Upper Deck", "Ice", "", ["Base", "Green /10", "Black /5", "Premieres"]),
    ("2024-25", "Upper Deck", "Premier", "", ["Base /299", "Gold /25", "Black /5"]),
    ("2024-25", "Upper Deck", "Stature", "", ["Base /399", "Red /35", "Black /5"]),
    ("2024-25", "Upper Deck", "SPx", "", ["Base", "Finite /199", "Auto"]),
    ("2024-25", "O-Pee-Chee", "OPC", "", ["Base", "Red Border", "Rainbow", "Black /100"]),
    ("2023-24", "O-Pee-Chee", "OPC", "", ["Base", "Red Border", "Rainbow"]),
    ("2024-25", "O-Pee-Chee", "Platinum", "", ["Base", "Sunset", "Rainbow", "Golden Treasures 1/1"]),
    ("2024-25", "Parkhurst", "Parkhurst", "", ["Base", "Red", "Gold /10"]),
    ("1990-91", "Score", "Score", "", ["Base"]),
    ("1990-91", "Upper Deck", "Upper Deck", "", ["Base"]),
    ("1991-92", "Upper Deck", "Upper Deck", "", ["Base"]),
    ("1993-94", "Upper Deck", "SP", "", ["Base", "Die-Cut"]),
    ("1994-95", "Upper Deck", "SP", "", ["Base"]),
    ("1995-96", "Upper Deck", "SP", "", ["Base"]),
    ("1996-97", "Upper Deck", "Series 1", "", ["Base"]),
    ("1997-98", "Pinnacle", "Be A Player", "", ["Base", "Autograph"]),
    ("2003-04", "Upper Deck", "Series 1", "Young Guns", ["Base"]),
    ("2004-05", "Upper Deck", "Series 1", "", ["Base"]),
    ("2023-24", "Upper Deck", "Starquest", "", ["Base", "Red", "Green", "Blue", "Gold", "Purple", "Orange", "Black /5", "Superfractor 1/1"]),
    ("2024-25", "Upper Deck", "Starquest", "", ["Base", "Red", "Green", "Blue", "Gold", "Purple", "Orange", "Black /5"]),
    ("2022-23", "Upper Deck", "Starquest", "", ["Red", "Green", "Blue", "Gold", "Purple"]),
    ("2024-25", "Upper Deck", "Choice", "", ["Base", "Reserve", "Starquest Red", "Starquest Green"]),
    ("2023-24", "Upper Deck", "Choice", "", ["Base", "Reserve"]),
    ("2024-25", "Upper Deck", "Splendor", "", ["Base /8", "Gold /3", "Black 1/1"]),
    ("2023-24", "Upper Deck", "Splendor", "", ["Base /8", "Gold /3"]),
    ("2024-25", "Upper Deck", "Trilogy", "", ["Base", "Red /999", "Gold /49", "Rookie Premieres"]),
    ("2024-25", "Upper Deck", "Ultimate", "", ["Base /399", "Gold /25", "Black /5"]),
    ("2024-25", "Upper Deck", "Black Diamond", "", ["Base", "Gem /99", "Quad Jersey"]),
    ("2014-15", "Upper Deck", "Series 1", "Young Guns", ["Base"]),
    ("2018-19", "Upper Deck", "Series 1", "Young Guns", ["Base", "Exclusives /100"]),
    ("2007-08", "Upper Deck", "Series 1", "Young Guns", ["Base"]),
    ("2008-09", "Upper Deck", "Series 1", "Young Guns", ["Base"]),
    ("2009-10", "Upper Deck", "Series 1", "Young Guns", ["Base"]),
    ("2010-11", "Upper Deck", "Series 1", "Young Guns", ["Base"]),
    ("2011-12", "Upper Deck", "Series 1", "Young Guns", ["Base"]),
    ("2012-13", "Upper Deck", "Series 1", "Young Guns", ["Base"]),
    ("2013-14", "Upper Deck", "Series 1", "Young Guns", ["Base"]),
    ("1993-94", "Fleer", "Ultra", "", ["Base", "Ultra Power", "Scoring Kings"]),
    ("1994-95", "Fleer", "Ultra", "", ["Base"]),
    ("1992-93", "Skybox", "Impact", "", ["Base", "Rookie"]),
    ("2024-25", "Upper Deck", "MVP", "", ["Base", "Gold Script", "Super Script"]),
    ("2024-25", "Upper Deck", "Chronology", "", ["Base", "Gold /25"]),
    ("2024-25", "Upper Deck", "Credentials", "", ["Base /99", "Gold /10"]),
]


def _tok(s):
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


def ensure_catalog(con: sqlite3.Connection):
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS catalog (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          year TEXT,
          brand TEXT,
          set_name TEXT,
          insert_name TEXT,
          parallel TEXT,
          player TEXT,
          number TEXT,
          team TEXT,
          print_run TEXT,
          source TEXT,
          created TEXT
        )
        """
    )
    con.execute("CREATE INDEX IF NOT EXISTS cat_player ON catalog(player)")
    con.execute("CREATE INDEX IF NOT EXISTS cat_set ON catalog(set_name)")
    con.execute("CREATE INDEX IF NOT EXISTS cat_year ON catalog(year)")
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS catalog_votes (
          user_id INTEGER NOT NULL,
          fp TEXT NOT NULL,
          year TEXT,
          set_name TEXT,
          insert_name TEXT,
          parallel TEXT,
          player TEXT,
          number TEXT,
          team TEXT,
          created TEXT,
          PRIMARY KEY (user_id, fp)
        )
        """
    )
    con.execute("CREATE INDEX IF NOT EXISTS cat_votes_fp ON catalog_votes(fp)")
    n = con.execute("SELECT COUNT(*) AS n FROM catalog WHERE source='seed'").fetchone()
    count = n["n"] if n else 0
    if count:
        return
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = []
    for year, brand, set_name, insert, pars in SETS:
        for p in pars:
            run = ""
            m = re.search(r"/\s*(\d+)|1/1", p)
            if m:
                run = m.group(0).replace(" ", "")
            rows.append((year, brand, set_name, insert or "", p, "", "", "", run, "seed", now))
    con.executemany(
        "INSERT INTO catalog(year,brand,set_name,insert_name,parallel,player,number,team,print_run,source,created) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )


def _fp(player, year, set_name, number, par, ins):
    return "|".join([
        _tok(player),
        _tok(year),
        _tok(set_name),
        _tok(number),
        _tok(par) or "base",
        _tok(ins),
    ])


def vote_card(con: sqlite3.Connection, user_id: int, card: dict):
    """One vote per user per fingerprint. Two distinct users promote it to catalog."""
    if not user_id:
        return
    player = (card.get("player") or "").strip()
    year = (card.get("year") or "").strip()
    set_name = (card.get("set") or "").strip()
    if len(player) < 3 or len(set_name) < 2:
        return
    if not re.search(r"[a-zA-Z]", player):
        return
    number = (card.get("number") or "").strip()
    par = (card.get("parallel") or "").strip() or "Base"
    ins = (card.get("insert") or "").strip()
    team = (card.get("team") or "").strip()
    fp = _fp(player, year, set_name, number, par, ins)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ")
    con.execute(
        """INSERT OR REPLACE INTO catalog_votes(user_id,fp,year,set_name,insert_name,parallel,player,number,team,created)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (user_id, fp, year, set_name, ins, par, player, number, team, now),
    )
    n = con.execute("SELECT COUNT(*) AS n FROM catalog_votes WHERE fp=?", (fp,)).fetchone()["n"]
    if n < 2:
        return
    hit = con.execute(
        """SELECT id FROM catalog WHERE lower(ifnull(player,''))=? AND year=? AND lower(set_name)=?
           AND ifnull(number,'')=? AND lower(ifnull(parallel,''))=? LIMIT 1""",
        (player.lower(), year, set_name.lower(), number, par.lower()),
    ).fetchone()
    if hit:
        return
    con.execute(
        "INSERT INTO catalog(year,brand,set_name,insert_name,parallel,player,number,team,print_run,source,created) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (year, "", set_name, ins, par, player, number, team, "", "voted", now),
    )


def learn_card(con: sqlite3.Connection, card: dict):
    return


def catalog_matches(con: sqlite3.Connection, data: dict, limit: int = 3):
    player = _tok(data.get("player"))
    year = str(data.get("year") or "").strip()
    st = _tok(data.get("set"))
    num = str(data.get("number") or "").strip()
    par = _tok(data.get("parallel"))
    ins = _tok(data.get("insert"))
    if not player and not st:
        return []
    rows = con.execute(
        """SELECT year, brand, set_name, insert_name, parallel, player, number, team, print_run, source
           FROM catalog
           WHERE (?='' OR player='' OR lower(player) LIKE ?)
             AND (?='' OR year='' OR year=?)
           LIMIT 400""",
        (player, f"%{player}%" if player else "%", year, year),
    ).fetchall()
    scored = []
    seen = set()
    for r in rows:
        set_t = _tok(r["set_name"])
        par_t = _tok(r["parallel"])
        ins_t = _tok(r["insert_name"])
        pl_t = _tok(r["player"])
        score = 0
        if player and player in pl_t:
            score += 8
        if year and r["year"] == year:
            score += 4
        if st and (st in set_t or set_t in st):
            score += 5
        if num and str(r["number"] or "") == num:
            score += 4
        if par and (par in par_t or par_t in par):
            score += 4
        if ins and (ins in ins_t or ins_t in ins):
            score += 3
        if "young gun" in ins or "young gun" in st:
            if "young gun" in ins_t:
                score += 2
        if score < 5:
            continue
        key = (r["year"], r["set_name"], r["insert_name"], r["parallel"], r["player"], r["number"])
        if key in seen:
            continue
        seen.add(key)
        label_bits = [r["year"], r["set_name"], r["insert_name"] or None, r["parallel"] if r["parallel"] not in ("", "Base") else None]
        if r["player"]:
            label_bits = [r["player"]] + label_bits
        if r["number"]:
            label_bits.append("#" + r["number"])
        scored.append(
            (
                score,
                {
                    "year": r["year"],
                    "set": r["set_name"],
                    "insert": r["insert_name"] or "",
                    "parallel": r["parallel"] or "",
                    "player": r["player"] or data.get("player") or "",
                    "number": r["number"] or data.get("number") or "",
                    "team": r["team"] or data.get("team") or "",
                    "label": " · ".join(x for x in label_bits if x),
                    "source": r["source"],
                    "score": score,
                },
            )
        )
    scored.sort(key=lambda x: -x[0])
    return [x[1] for x in scored[:limit]]


def catalog_stats(con: sqlite3.Connection):
    tot = con.execute("SELECT COUNT(*) AS n FROM catalog").fetchone()["n"]
    seed = con.execute("SELECT COUNT(*) AS n FROM catalog WHERE source='seed'").fetchone()["n"]
    voted = con.execute("SELECT COUNT(*) AS n FROM catalog WHERE source='voted'").fetchone()["n"]
    pending = con.execute("SELECT COUNT(DISTINCT fp) AS n FROM catalog_votes").fetchone()["n"]
    return {"rows": tot, "seed": seed, "voted": voted, "pending": pending}
