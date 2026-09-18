#!/usr/bin/env python3
"""
Detroit Tigers final-score notifier, with Hao-Yu Lee stat line.

When the Tigers' game goes Final, sends ONE push notification via ntfy with:
  - the game result
  - Lee's batting line in that game
  - his season slash + advanced rates (official MLB API)
  - his FanGraphs line: wOBA, wRC+, WAR
  - his Statcast line: xwOBA, exit velocity, Barrel%, HardHit% (Baseball Savant)
Records each game so you're never pinged twice.

Standard library only. Environment variables:
  NTFY_TOPIC   (required)  your secret ntfy topic name
  NTFY_SERVER  (optional)  defaults to https://ntfy.sh

Note: the official MLB API is stable. FanGraphs and Baseball Savant have no
official public API, so those two lines rely on undocumented endpoints that
may occasionally change. If either breaks, that line simply drops out and the
rest of the notification still sends. Set INCLUDE_STATCAST = False to turn the
FanGraphs + Savant lines off entirely.
"""

import csv
import io
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone, timedelta

TEAM_ID = 116                      # Detroit Tigers
PLAYER_ID = 701678                 # Hao-Yu Lee
PLAYER_NAME = "Hao-Yu Lee"
INCLUDE_STATCAST = True            # FanGraphs + Baseball Savant lines
STATE_FILE = "notified.json"
KEEP_LAST = 30

NTFY_TOPIC = os.environ.get("NTFY_TOPIC")
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
API = "https://statsapi.mlb.com/api/v1"
UA = {"User-Agent": "Mozilla/5.0 (compatible; tigers-notifier/1.0)"}


def log(msg):
    print(msg, flush=True)


# ------------------------- state -------------------------

def load_notified():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_notified(ids):
    with open(STATE_FILE, "w") as f:
        json.dump(ids[-KEEP_LAST:], f)


# ------------------------- http --------------------------

def fetch_json(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def fetch_text(url, timeout=45):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


# ------------------------- formatting --------------------

def slash(x):
    """Rate stat in baseball style: .265, 1.000, or — if missing."""
    if x in (None, "", "-.--", ".---"):
        return "\u2014"
    try:
        v = float(x)
    except (TypeError, ValueError):
        return str(x)
    s = f"{v:.3f}"
    if s.startswith("0."):
        s = s[1:]
    elif s.startswith("-0."):
        s = "-" + s[2:]
    return s


def pct(numer, denom):
    try:
        return f"{100.0 * float(numer) / float(denom):.1f}"
    except (TypeError, ValueError, ZeroDivisionError):
        return "\u2014"


def one(x):
    try:
        return f"{float(x):.1f}"
    except (TypeError, ValueError):
        return "\u2014"


def whole(x):
    try:
        return str(int(round(float(x))))
    except (TypeError, ValueError):
        return "\u2014"


def num(x):
    try:
        return int(x)
    except (TypeError, ValueError):
        try:
            return float(x)
        except (TypeError, ValueError):
            return 0


def pick(row, *names):
    """First present, non-empty value among candidate column names."""
    for n in names:
        if n in row and str(row[n]).strip() not in ("", "null", "None"):
            return row[n]
    return None


# ------------------------- schedule ----------------------

def fetch_schedule():
    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    end = now.strftime("%Y-%m-%d")
    return fetch_json(f"{API}/schedule?sportId=1&teamId={TEAM_ID}"
                      f"&startDate={start}&endDate={end}")


def final_games(schedule):
    for date in schedule.get("dates", []):
        for game in date.get("games", []):
            status = game.get("status", {})
            if status.get("abstractGameState") != "Final":
                continue
            detailed = status.get("detailedState", "")
            if any(w in detailed for w in
                   ("Postponed", "Suspended", "Cancelled", "Canceled")):
                continue

            teams = game.get("teams", {})
            away, home = teams.get("away", {}), teams.get("home", {})
            a_score, h_score = away.get("score"), home.get("score")
            if a_score is None or h_score is None:
                continue

            a_name = away.get("team", {}).get("name", "Away")
            h_name = home.get("team", {}).get("name", "Home")
            if TEAM_ID == away.get("team", {}).get("id"):
                det, opp, opp_name = a_score, h_score, h_name
            else:
                det, opp, opp_name = h_score, a_score, a_name

            verb = "win" if det > opp else "lose" if det < opp else "tie"
            body = f"\u26be Detroit {det}\u2013{opp} vs {opp_name} (Final)"
            season = game.get("season") or datetime.now(timezone.utc).year
            yield game["gamePk"], season, body, verb


# ------------------------- player: game line -------------

def player_game_line(game_pk):
    try:
        box = fetch_json(f"{API}/game/{game_pk}/boxscore")
    except Exception as e:
        log(f"  (couldn't fetch boxscore: {e})")
        return None

    key = f"ID{PLAYER_ID}"
    for side in ("home", "away"):
        players = box.get("teams", {}).get(side, {}).get("players", {})
        if key in players:
            bat = players[key].get("stats", {}).get("batting", {})
            ab = num(bat.get("atBats"))
            bb = num(bat.get("baseOnBalls"))
            hbp = num(bat.get("hitByPitch"))
            sf = num(bat.get("sacFlies"))
            if ab + bb + hbp + sf == 0:
                return "did not play"
            parts = [f"{num(bat.get('hits'))}-for-{ab}"]
            for stat, label in (("doubles", "2B"), ("triples", "3B"),
                                ("homeRuns", "HR"), ("rbi", "RBI"),
                                ("runs", "R")):
                if num(bat.get(stat)):
                    parts.append(f"{num(bat.get(stat))} {label}")
            if bb:
                parts.append(f"{bb} BB")
            if num(bat.get("strikeOuts")):
                parts.append(f"{num(bat.get('strikeOuts'))} K")
            if num(bat.get("stolenBases")):
                parts.append(f"{num(bat.get('stolenBases'))} SB")
            return ", ".join(parts)
    return "did not play"


# ------------------------- player: season (MLB API) ------

def first_stat(data):
    try:
        return data["stats"][0]["splits"][0]["stat"]
    except (KeyError, IndexError, TypeError):
        return {}


def player_season(season):
    try:
        basic = first_stat(fetch_json(
            f"{API}/people/{PLAYER_ID}/stats"
            f"?stats=season&group=hitting&season={season}"))
    except Exception as e:
        log(f"  (couldn't fetch season stats: {e})")
        return None, None
    if not basic:
        return None, None

    avg, obp, slg, ops = (basic.get("avg"), basic.get("obp"),
                          basic.get("slg"), basic.get("ops"))
    slash_line = f"{slash(avg)}/{slash(obp)}/{slash(slg)}, {slash(ops)} OPS"

    try:
        iso = slash(float(slg) - float(avg))
    except (TypeError, ValueError):
        iso = "\u2014"

    pa = basic.get("plateAppearances")
    bb_pct = pct(basic.get("baseOnBalls"), pa)
    k_pct = pct(basic.get("strikeOuts"), pa)

    babip = "\u2014"
    try:
        adv = first_stat(fetch_json(
            f"{API}/people/{PLAYER_ID}/stats"
            f"?stats=seasonAdvanced&group=hitting&season={season}"))
        if adv.get("babip") not in (None, ""):
            babip = slash(adv.get("babip"))
    except Exception:
        pass
    if babip == "\u2014":
        try:
            h = float(basic.get("hits")); hr = float(basic.get("homeRuns"))
            ab = float(basic.get("atBats")); k = float(basic.get("strikeOuts"))
            sf = float(basic.get("sacFlies") or 0)
            babip = slash((h - hr) / (ab - k - hr + sf))
        except (TypeError, ValueError, ZeroDivisionError):
            pass

    return slash_line, f"ISO {iso}, BABIP {babip}, BB% {bb_pct}, K% {k_pct}"


# ------------------------- player: FanGraphs -------------

def fangraphs_line(season):
    url = ("https://www.fangraphs.com/api/leaders/major-league/data"
           "?age=&pos=all&stats=bat&lg=all&qual=0"
           f"&season={season}&season1={season}"
           "&month=0&hand=&team=0&pageitems=2000000000&pagenum=1"
           "&ind=0&rost=0&players=&type=8&postseason=&sortdir=default"
           "&sortstat=WAR")
    try:
        data = json.loads(fetch_text(url))
    except Exception as e:
        log(f"  (FanGraphs fetch failed: {e})")
        return None
    rows = data.get("data") if isinstance(data, dict) else data
    for row in rows or []:
        if (str(row.get("xMLBAMID")) == str(PLAYER_ID)
                or str(row.get("Name", "")).strip() == PLAYER_NAME):
            return (f"wOBA {slash(row.get('wOBA'))}, "
                    f"wRC+ {whole(row.get('wRC+'))}, "
                    f"WAR {one(row.get('WAR'))}")
    log("  (FanGraphs: player not found in results)")
    return None


# ------------------------- player: Statcast (Savant) -----

def _savant_row(url):
    try:
        text = fetch_text(url)
    except Exception as e:
        log(f"  (Savant fetch failed: {e})")
        return None
    try:
        for row in csv.DictReader(io.StringIO(text)):
            if str(row.get("player_id", "")).strip() == str(PLAYER_ID):
                return row
    except Exception as e:
        log(f"  (Savant parse failed: {e})")
    return None


def statcast_line(season):
    exp = _savant_row(
        "https://baseballsavant.mlb.com/leaderboard/expected_statistics"
        f"?type=batter&year={season}&position=&team=&filterType=bip"
        "&min=1&csv=true")
    ev = _savant_row(
        "https://baseballsavant.mlb.com/leaderboard/exit_velocity"
        f"?type=batter&year={season}&position=&team=&min=1&csv=true")

    parts = []
    if exp:
        xwoba = pick(exp, "est_woba", "xwoba", "xwOBA")
        if xwoba is not None:
            parts.append(f"xwOBA {slash(xwoba)}")
    if ev:
        v = pick(ev, "avg_hit_speed", "avg_exit_velocity", "exit_velocity_avg")
        if v is not None:
            parts.append(f"EV {one(v)}")
        brl = pick(ev, "brl_percent", "barrel_batted_rate",
                   "barrels_per_bbe_percent")
        if brl is not None:
            parts.append(f"Barrel% {one(brl)}")
        hh = pick(ev, "ev95percent", "hard_hit_percent", "hardhitpercent",
                  "hard_hit_rate")
        if hh is not None:
            parts.append(f"HardHit% {one(hh)}")
    return ", ".join(parts) if parts else None


# ------------------------- ntfy --------------------------

def send_ntfy(title, body):
    if not NTFY_TOPIC:
        log("ERROR: NTFY_TOPIC is not set. Nothing sent.")
        sys.exit(1)
    req = urllib.request.Request(
        f"{NTFY_SERVER}/{NTFY_TOPIC}",
        data=body.encode("utf-8"),
        headers={"Title": title, "Priority": "default", "Tags": "baseball"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        resp.read()


# ------------------------- main --------------------------

def main():
    notified = load_notified()
    try:
        schedule = fetch_schedule()
    except Exception as e:
        log(f"Could not reach the MLB API: {e}")
        sys.exit(1)

    new = 0
    for game_pk, season, score_body, verb in final_games(schedule):
        if game_pk in notified:
            continue
        log(f"New final game {game_pk}: {score_body}")

        lines = [score_body, ""]
        game_line = player_game_line(game_pk)
        if game_line:
            lines.append(f"{PLAYER_NAME} today: {game_line}")
        slash_line, advanced_line = player_season(season)
        if slash_line:
            lines.append(f"Season: {slash_line}")
        if advanced_line:
            lines.append(f"Advanced: {advanced_line}")
        if INCLUDE_STATCAST:
            fg = fangraphs_line(season)
            if fg:
                lines.append(f"FanGraphs: {fg}")
            sc = statcast_line(season)
            if sc:
                lines.append(f"Statcast: {sc}")

        send_ntfy(f"Tigers {verb}!", "\n".join(lines))
        notified.append(game_pk)
        new += 1

    if new:
        save_notified(notified)
        log(f"Sent {new} notification(s).")
    else:
        log("No new final games. Nothing to send.")


if __name__ == "__main__":
    main()
