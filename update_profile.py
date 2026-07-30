"""Regenerate dark_mode.svg / light_mode.svg with live GitHub stats.

Runs daily via GitHub Actions. Stdlib only, no dependencies.
"""
import calendar
import html
import json
import os
import urllib.request
from datetime import date, datetime, timezone

USER = "adrian-villafana"
BIRTHDAY = date(1988, 5, 14)
JOINED_YEAR = 2024  # account creation year, never changes
W = 56  # info column width in characters

ART = r"""
..:++++++=======-:-----------:::::::..
.::+++++++++--:..  .:::-------::::::..
.::=++++++-.           .------:::::::.
.::+*++*+:              .----:::::::..
:::+***+-                .----:::::::.
:::+*+*-        -:        :---::::::..
:::+***.   .=**#%%-=+-:    --:::::::..
:::=*+*.  :*#%@@@@@@@@#+.  ----::::::.
:::=*+*:  -##%@@@@@@@@@%=  ----::::::.
:::=+++=  -#%@@@@@@@@@@%= .----:::::..
:::=++++. +*#%@@@@@@@@@%= :=---:::::..
:::=++++.-=::--=#@@#++-:-.-=---:::::..
:::=+++*-*#+=-=+*@%+-:==*-=----:::::..
.::=+++*=*@%#%@@%@@@@###%===---::::...
:::=++++**@@@@@@#@@@@@@@#===--::::...
:::=+++***@@@@@##@@%@@@@*===--:::...
:::-**+==-%@@@@*%@@%@@@%::::-:::...
:::-=-:.  *%@@%##%%%@@@+.::::..:..
:::...... -#%%*#%%%%%@#-.:-:..:::.
:.::...... -#%*+##**%*-::.:.::::::.
...:::....  =*######*=.::::::.:::::.
:..::...... :==#@@%++-.:..:..::::.:.:
.:..:...... .=--++=+-..:..:.:::..:.::.
....:::......=**+**-...::..:.:....::..
..:..::..:....=**+:.....:..:.....:....
.::..::..:.... ............... .......
..:..::..:..................   ....:..
"""

# two tokens by design: the Actions GITHUB_TOKEN yields the contribution-style
# commit count (public + private activity), while a PAT (ACCESS_TOKEN secret)
# sees private repos for the repo list, language and LOC walk. Either falls back to the other.
TOKEN = os.environ.get("GITHUB_TOKEN") or os.environ.get("ACCESS_TOKEN") or ""
PRIV_TOKEN = os.environ.get("ACCESS_TOKEN") or TOKEN


def gh(url, payload=None, token=None):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode() if payload else None,
        headers={"Authorization": f"Bearer {token or TOKEN}", "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(req) as r:
        return r.status, json.loads(r.read() or "{}")


def graphql(query, variables=None, token=None):
    _, resp = gh("https://api.github.com/graphql", {"query": query, "variables": variables or {}}, token)
    if resp.get("errors"):
        raise RuntimeError(resp["errors"])
    return resp["data"]


def age(b, t):
    years = t.year - b.year - ((t.month, t.day) < (b.month, b.day))
    months = (t.month - b.month - (t.day < b.day)) % 12
    if t.day >= b.day:
        days = t.day - b.day
    else:
        pm_year, pm = (t.year, t.month - 1) if t.month > 1 else (t.year - 1, 12)
        days = calendar.monthrange(pm_year, pm)[1] - b.day + t.day
    return years, months, days


# languages we don't want to advertise, and the punchline appended either way
LANG_BLOCKLIST = {"Ruby"}
LANG_PUNCHLINE = "(Ruby is not invited)"


def top_languages(nodes, limit=4):
    # rank by how many repos have each language as their primary one
    freq = {}
    for n in nodes:
        pl = n.get("primaryLanguage")
        if not pl:
            continue
        name = pl["name"]
        if name in LANG_BLOCKLIST:
            continue
        freq[name] = freq.get(name, 0) + 1
    ranked = sorted(freq, key=freq.get, reverse=True)
    # Go is always pinned first; fill the rest from detected languages
    ordered = ["Go"] + [l for l in ranked if l != "Go"]
    return f"{', '.join(ordered[:limit])} {LANG_PUNCHLINE}"


def fetch_stats():
    yr_aliases = "\n".join(
        f'y{y}: contributionsCollection(from: "{y}-01-01T00:00:00Z", to: "{y + 1}-01-01T00:00:00Z")'
        " { totalCommitContributions restrictedContributionsCount }"
        for y in range(JOINED_YEAR, datetime.now(timezone.utc).year + 1)
    )
    contrib = graphql(f'query {{ user(login: "{USER}") {{ {yr_aliases} }} }}')["user"]
    commits = sum(
        v["totalCommitContributions"] + v["restrictedContributionsCount"]
        for v in contrib.values()
    )
    u = graphql(f"""
    query {{
      user(login: "{USER}") {{
        id
        followers {{ totalCount }}
        repositories(first: 100, ownerAffiliations: OWNER) {{
          totalCount
          nodes {{ nameWithOwner stargazerCount isFork primaryLanguage {{ name }} }}
        }}
        repositoriesContributedTo(first: 100, includeUserRepositories: true,
            contributionTypes: [COMMIT, PULL_REQUEST, REPOSITORY]) {{
          totalCount
          nodes {{ nameWithOwner isFork primaryLanguage {{ name }} }}
        }}
      }}
    }}""", token=PRIV_TOKEN)["user"]
    owned = u["repositories"]["nodes"]
    contributed = u["repositoriesContributedTo"]["nodes"]
    # LOC + languages span everything you touch (owned + org repos via kueski-dev),
    # deduped by owner/name; commit walk is author-filtered so only your lines count
    repos = {}
    for n in owned + contributed:
        if n["isFork"]:
            continue
        owner, name = n["nameWithOwner"].split("/", 1)
        repos[n["nameWithOwner"]] = (owner, name)
    stats = {
        "followers": u["followers"]["totalCount"],
        "repos": u["repositories"]["totalCount"],
        "contributed": u["repositoriesContributedTo"]["totalCount"],
        "stars": sum(n["stargazerCount"] for n in owned),
        "commits": commits,
        "languages": top_languages(owned + contributed),
    }
    stats.update(loc(list(repos.values()), u["id"]))
    return stats


LOC_QUERY = """
query($owner: String!, $name: String!, $id: ID!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    defaultBranchRef { target { ... on Commit {
      history(first: 100, author: {id: $id}, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes { additions deletions }
      }
    } } }
  }
}"""


def loc(repos, user_id):
    # REST stats/contributors answers 202 forever to the Actions token,
    # so walk own commits on the default branch via GraphQL instead
    add = rem = 0
    for owner, name in repos:
        cursor = None
        try:
            while True:
                ref = graphql(LOC_QUERY, {"owner": owner, "name": name, "id": user_id, "cursor": cursor}, token=PRIV_TOKEN)["repository"]["defaultBranchRef"]
                if ref is None:
                    break  # empty repo
                h = ref["target"]["history"]
                add += sum(n["additions"] for n in h["nodes"])
                rem += sum(n["deletions"] for n in h["nodes"])
                if not h["pageInfo"]["hasNextPage"]:
                    break
                cursor = h["pageInfo"]["endCursor"]
        except Exception as e:
            print(f"loc {name}: {e}")
    return {"loc_add": add, "loc_del": rem, "loc": add - rem}


PALETTES = {
    "dark": {"bg": "#0d1117", "border": "#30363d", "art": "#8b949e", "h": "#58a6ff",
             "k": "#ffa657", "v": "#c9d1d9", "d": "#484f58", "g": "#3fb950", "r": "#f85149"},
    "light": {"bg": "#ffffff", "border": "#d0d7de", "art": "#57606a", "h": "#0969da",
              "k": "#953800", "v": "#24292f", "d": "#afb8c1", "g": "#1a7f37", "r": "#cf222e"},
}


def kv(key, val, width=W):
    dots = "." * max(width - len(key) - len(str(val)) - 3, 1)
    return [(f"{key}: ", "k"), (dots + " ", "d"), (str(val), "v")]


def kv2(k1, v1, k2, v2):
    left = kv(k1, v1, 30)
    return left + [(" | ", "d")] + kv(k2, v2, 23)


def rule(title=""):
    label = f"─ {title} " if title else ""
    return [(label, "h"), ("─" * (W - len(label)), "d")]


def info_lines(s):
    y, m, d = age(BIRTHDAY, date.today())
    n = lambda x: f"{x:,}"
    return [
        [(f"{USER}@github ", "h"), ("─" * (W - len(USER) - 8), "d")],
        [],
        kv("OS", "macOS, Linux"),
        kv("Uptime", f"{y} years, {m} months, {d} days"),
        kv("Host", "Kueski (FTE, not a contractor)"),
        kv("Kernel", "Software Engineer (Go or go home)"),
        kv("IDE", "Claude Code, Cursor, Codex (sometimes I code too)"),
        [],
        kv("Languages", s["languages"]),
        kv("Hobbies", "Chess (lichess.org running in background)"),
        [],
        rule("Contact"),
        kv("Email", "adnvilla@gmail.com"),
        kv("LinkedIn", "in/adrian-villafana"),
        kv("Blog", "adrianvillafana.com"),
        [],
        rule("GitHub Stats"),
        kv2("Repos", f"{s['repos']} {{Contributed: {s['contributed']}}}", "Stars", n(s["stars"])),
        kv2("Commits", n(s["commits"]), "Followers", n(s["followers"])),
        [("Lines of Code: ", "k"), (n(s["loc"]), "v"), (" ( ", "d"),
         (n(s["loc_add"]) + "++", "g"), (", ", "d"), (n(s["loc_del"]) + "--", "r"), (" )", "d")],
    ]


def render(mode, stats):
    p = PALETTES[mode]
    out = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="840" height="500" viewBox="0 0 840 500" '
        f'font-family="Consolas, Menlo, monospace" font-size="13px">',
        f'<rect x="0.5" y="0.5" width="839" height="499" rx="10" fill="{p["bg"]}" stroke="{p["border"]}"/>',
    ]
    for i, line in enumerate(ART.strip("\n").split("\n")):
        out.append(f'<text x="25" y="{40 + i * 15}" fill="{p["art"]}" xml:space="preserve">{html.escape(line)}</text>')
    for i, segs in enumerate(info_lines(stats)):
        if not segs:
            continue
        spans = "".join(f'<tspan fill="{p[c]}">{html.escape(t)}</tspan>' for t, c in segs)
        out.append(f'<text x="390" y="{45 + i * 21}" xml:space="preserve">{spans}</text>')
    out.append("</svg>")
    return "\n".join(out)


def selfcheck():
    assert age(date(1989, 1, 15), date(2026, 7, 10)) == (37, 5, 25)
    assert age(date(2000, 3, 31), date(2026, 4, 1)) == (26, 0, 1)
    assert age(date(2000, 1, 1), date(2026, 1, 1)) == (26, 0, 0)
    assert len("".join(t for t, _ in kv("OS", "Windows, macOS"))) == W


if __name__ == "__main__":
    selfcheck()
    stats = fetch_stats()
    print("stats:", stats)
    for mode in PALETTES:
        with open(f"{mode}_mode.svg", "w", encoding="utf-8") as f:
            f.write(render(mode, stats))
    print("wrote dark_mode.svg, light_mode.svg")
