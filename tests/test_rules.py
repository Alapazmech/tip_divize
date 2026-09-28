"""Pravidla vypořádání — spustit před commitem: python3 -m unittest discover tests"""

import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import generate_site as gs  # noqa: E402
import scraper  # noqa: E402
import tickets  # noqa: E402


def m(i, rnd=4, score=None, **kw):
    base = {
        "id": i, "round": rnd, "date": "2026-10-03", "time": None,
        "home": f"H{i}", "away": f"A{i}", "home_short": None, "away_short": None,
        "score": score, "overtime": False, "shootout": False,
    }
    return {**base, **kw}


MATCHES = [m(1, score=[2, 0]), m(2), m(3, score=[1, 1]), m(4, score=[3, 2], overtime=True)]
PUB = {
    str(i): {"round": 4, "match_id": i, "odds": {"1": 1.5, "0": 4.0, "2": 3.0}}
    for i in (1, 2, 3, 4)
}
BY_ID = {x["id"]: x for x in MATCHES}


def bets(rows: str) -> pathlib.Path:
    path = pathlib.Path(tempfile.mkdtemp()) / "bets.csv"
    path.write_text("round,person,ticket,match,market,stake\n" + rows)
    return path


class Settle(unittest.TestCase):
    def test_lost_leg_settles_immediately(self):
        st = gs.settle(MATCHES, PUB, bets("4,X,a,1,2,20\n4,X,a,2,1,20\n"))
        (t,) = st["settled"][4]
        self.assertFalse(t["won"])
        self.assertEqual(t["delta"], -20)
        self.assertEqual(t["leg_wins"], [False, None])
        self.assertEqual(st["banks"]["X"], 80)
        self.assertEqual(st["open"], {})

    def test_won_leg_waits_for_rest(self):
        st = gs.settle(MATCHES, PUB, bets("4,Y,b,1,1,30\n4,Y,b,2,1,30\n"))
        self.assertEqual(st["settled"], {})
        self.assertEqual([t["person"] for t in st["open"][4]], ["Y"])
        self.assertEqual(st["banks"]["Y"], 100)

    def test_overtime_is_draw(self):
        st = gs.settle(MATCHES, PUB, bets("4,Z,c,4,0,10\n"))
        (t,) = st["settled"][4]
        self.assertTrue(t["won"])
        self.assertEqual(t["delta"], 30)

    def test_in_play(self):
        path = bets("4,Y,b,1,1,30\n4,Y,b,2,1,30\n")
        path.with_name("bets_sealed.json").write_text('[{"person": "Q", "stake": 15}]')
        st = gs.settle(MATCHES, PUB, path)
        self.assertEqual(gs.in_play_stakes(st, path), {"Y": 30, "Q": 15})


class Decided(unittest.TestCase):
    def T(self, *legs):
        return {"legs": [{"match_id": i, "market": k} for i, k in legs]}

    def test_rules(self):
        self.assertTrue(tickets.decided(self.T((1, "2"), (2, "1")), BY_ID))  # prohraný leg
        self.assertFalse(tickets.decided(self.T((1, "1"), (2, "1")), BY_ID))  # čeká
        self.assertFalse(tickets.decided(self.T((1, "1"), (99, "1")), BY_ID))  # chybí zápas
        self.assertTrue(tickets.decided(self.T((1, "1"), (3, "0")), BY_ID))  # vše dohráno


class Payout(unittest.TestCase):
    def P(self, banks):
        return gs.payout_now({"pot_kc": 1100, "banks": banks})

    def test_sum_and_ties(self):
        out = self.P({"A": 165, "B": 100, "C": 100, "D": 50})
        self.assertEqual(out, {"A": 684, "B": 208, "C": 208})
        self.assertEqual(sum(out.values()), 1100)
        self.assertEqual(self.P({"A": 100, "B": 100, "C": 50}), {"A": 550, "B": 550})
        self.assertEqual(sum(self.P({"A": 100, "B": 100, "C": 100}).values()), 1100)
        self.assertEqual(self.P({"A": 300, "B": 100}), {"A": 825, "B": 275})
        self.assertEqual(self.P({"A": 100, "B": 0}), {"A": 1100})


class Scraper(unittest.TestCase):
    def test_keep_missing(self):
        merged = scraper.keep_missing([m(1), m(3)], MATCHES)
        self.assertEqual(sorted(x["id"] for x in merged), [1, 2, 3, 4])
        self.assertEqual(scraper.keep_missing([m(1)], [m(1)]), [m(1)])


if __name__ == "__main__":
    unittest.main()
