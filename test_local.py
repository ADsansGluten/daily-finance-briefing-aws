import os, sys, types, json
from unittest import mock
from datetime import datetime, timezone, timedelta
from email.utils import format_datetime

# --- faux boto3 (pas besoin d'AWS pour tester) ---
calls = {"s3": [], "ses": [], "bedrock": []}
class FakeClient:
    def __init__(self, kind): self.kind = kind
    def put_object(self, **kw): calls["s3"].append(kw["Key"])
    def send_email(self, **kw): calls["ses"].append(kw)
    def converse(self, **kw):
        calls["bedrock"].append(kw)
        text = "## À retenir\n- **Le CAC 40** progresse [1]\n- La BCE maintient ses taux [2]\n## Marchés et entreprises\n- Rien de notable.\n"
        return {"output": {"message": {"content": [{"reasoningContent": {"x": 1}}, {"text": text}]}},
                "usage": {"inputTokens": 900, "outputTokens": 120}}
fake = types.ModuleType("boto3"); fake.client = lambda kind, *a, **k: FakeClient(kind)
sys.modules["boto3"] = fake

sys.path.insert(0, ".")
import lambda_function as lf

now = datetime.now(timezone.utc)
rss = f"""<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Le CAC 40 progresse</title><description>&lt;p&gt;Séance <b>positive</b>&lt;/p&gt;</description><link>https://ex.fr/1</link><pubDate>{format_datetime(now - timedelta(hours=2))}</pubDate></item>
<item><title>Le CAC 40 progresse</title><description>doublon</description><link>https://ex.fr/1b</link><pubDate>{format_datetime(now - timedelta(hours=3))}</pubDate></item>
<item><title>Vieil article</title><description>trop ancien</description><link>https://ex.fr/old</link><pubDate>{format_datetime(now - timedelta(hours=90))}</pubDate></item>
</channel></rss>"""
atom = f"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>La BCE maintient ses taux</title><summary>Décision</summary><link href="https://ex.fr/2"/><updated>{(now - timedelta(hours=1)).isoformat()}</updated></entry></feed>"""

class Resp:
    def __init__(s, b): s.b = b
    def read(s): return s.b
    def __enter__(s): return s
    def __exit__(s, *a): pass
def fake_urlopen(req, timeout=10):
    u = req.full_url
    if "broken" in u: raise OSError("panne simulée")
    return Resp((atom if "atom" in u else rss).encode())

os.environ.update(BUCKET="b", SENDER="a@b.fr", RECIPIENT="a@b.fr",
                  FEEDS="RSS|https://x/rss;Atom|https://x/atom;Cassé|https://x/broken")
with mock.patch("urllib.request.urlopen", fake_urlopen):
    r_dry = lf.lambda_handler({"dry_run": True}, None)
    assert r_dry["articles"] == 2, r_dry          # doublon + vieil article éliminés
    assert not calls["ses"], "dry_run ne doit pas envoyer d'e-mail"
    n_bedrock = len(calls["bedrock"])
    r_noai = lf.lambda_handler({"dry_run": True, "no_ai": True}, None)
    assert r_noai["no_ai"] and len(calls["bedrock"]) == n_bedrock, "no_ai ne doit pas appeler Bedrock"
    r = lf.lambda_handler({}, None)
assert len(calls["ses"]) == 1
body = calls["ses"][0]["Content"]["Simple"]["Body"]["Html"]["Data"]
assert "<h3" in body and "<li" in body and "**" not in body and "https://ex.fr/2" in body
assert "Le CAC 40" in calls["bedrock"][0]["messages"][0]["content"][0]["text"]
assert any(k.endswith("latest.html") for k in calls["s3"]) and any(k.endswith(".json") for k in calls["s3"])
print("OK", r, sorted(set(calls["s3"])))
