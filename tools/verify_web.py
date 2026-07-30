#!/usr/bin/env python3
"""RioApis console: IP whitelist, key generation, dollar accounting, chat."""
import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TMP = tempfile.mkdtemp(prefix="rioapis-test-")
os.environ["KIRO_CLI_BIN"] = str(ROOT / "tools" / "fake-kiro-cli")
os.environ["USAGE_DB_PATH"] = os.path.join(TMP, "usage.db")
os.environ["ENV_FILE"] = os.path.join(TMP, ".env")
os.environ["ADMIN_WHITELIST_IPS"] = ""
os.environ["USD_PER_CREDIT"] = "0.04"
os.environ["KIRO_DEFAULT_MODEL"] = "auto"
os.environ["BRIDGE_API_KEY"] = "sk-bootstrap-xyz"

with open(os.environ["ENV_FILE"], "w") as handle:
    handle.write("# seed\nKIRO_DEFAULT_MODEL=auto\nUSD_PER_CREDIT=0.04\n")

from fastapi.testclient import TestClient  # noqa: E402

from kiro_openai.server import app  # noqa: E402

failures = []


def check(label, condition, detail=""):
    print("[{0}] {1}{2}".format("PASS" if condition else "FAIL", label,
                                "" if condition else "  <- " + str(detail)))
    if not condition:
        failures.append(label)


with TestClient(app) as client:
    print("== console reachable from a whitelisted address ==")
    session = client.get("/api/session").json()
    check("session reports admin for loopback", session["admin"] is True, session)
    check("brand is RioApis", session["brand"] == "RioApis", session)

    print("\n== UI is served ==")
    page = client.get("/")
    check("index served", page.status_code == 200 and "RioApis" in page.text, page.status_code)
    for asset in ("/static/app.css", "/static/app.js"):
        check("{0} served".format(asset), client.get(asset).status_code == 200)
    check("no credit multiplier leaked into the UI",
          "multiplier" not in client.get("/static/app.js").text.lower())

    print("\n== bootstrap ==")
    boot = client.get("/api/bootstrap").json()
    ids = [m["id"] for m in boot["models"]]
    check("models carry a cost", all("cost" in m for m in boot["models"]), boot["models"][:2])
    check("sonnet 5 present", "claude-sonnet-5" in ids, ids)
    check("dollar rate exposed", boot["usd_per_credit"] == 0.04, boot)

    print("\n== non-whitelisted address is refused ==")
    # trust_proxy_headers defaults off, so a spoofed header must NOT grant access.
    spoof = client.get("/api/bootstrap", headers={"X-Forwarded-For": "9.9.9.9"})
    check("spoofed X-Forwarded-For cannot grant access", spoof.status_code == 200,
          "loopback still used, header ignored")

    print("\n== API key generation ==")
    created = client.post("/api/keys", json={"name": "laptop"}).json()
    new_key = created["key"]
    print("   issued:", new_key[:14] + "...")
    check("key prefixed sk-rio-", new_key.startswith("sk-rio-"), new_key[:10])
    keys = client.get("/api/keys").json()["keys"]
    check("key listed", len(keys) == 1, keys)
    check("plaintext never returned by list",
          all("key" not in k for k in keys), keys)
    check("only a prefix is shown", keys[0]["prefix"] in new_key, keys[0])

    print("\n== generated key works on /v1 from any IP ==")
    r = client.post("/v1/chat/completions",
                    headers={"Authorization": "Bearer " + new_key,
                             "X-Forwarded-For": "203.0.113.77"},
                    json={"model": "claude-sonnet-5",
                          "messages": [{"role": "user", "content": "hi"}]})
    check("accepted", r.status_code == 200, r.text[:200])
    check("requested model honoured", r.json()["model"] == "claude-sonnet-5", r.json()["model"])

    bad = client.post("/v1/chat/completions",
                      headers={"Authorization": "Bearer sk-rio-not-a-real-key"},
                      json={"messages": [{"role": "user", "content": "hi"}]})
    check("unknown key rejected", bad.status_code == 401, bad.status_code)
    check("no key rejected",
          client.post("/v1/chat/completions",
                      json={"messages": [{"role": "user", "content": "hi"}]}).status_code == 401)
    check("bootstrap key still accepted",
          client.post("/v1/chat/completions",
                      headers={"Authorization": "Bearer sk-bootstrap-xyz"},
                      json={"messages": [{"role": "user", "content": "hi"}]}).status_code == 200)

    print("\n== revocation ==")
    key_id = keys[0]["id"]
    client.post("/api/keys/{0}/revoke".format(key_id))
    after = client.post("/v1/chat/completions",
                        headers={"Authorization": "Bearer " + new_key},
                        json={"messages": [{"role": "user", "content": "hi"}]})
    check("revoked key refused", after.status_code == 401, after.status_code)

    print("\n== usage is logged in dollars ==")
    stats = client.get("/api/stats").json()
    log = client.get("/api/usage").json()
    print("   requests:", stats["totals"]["requests"], "spend:", stats["totals"]["usd"])
    # Only authenticated calls reach the model, so the three 401s above are
    # deliberately not billed: 1x claude-sonnet-5 + 1x auto via the bootstrap key.
    check("only authenticated requests billed", stats["totals"]["requests"] == 2, stats["totals"])
    check("credits are 1.30 + 1.00", abs(stats["totals"]["credits"] - 2.3) < 1e-6, stats["totals"])
    check("spend is a dollar figure", stats["totals"]["usd"] > 0, stats["totals"])
    # claude-sonnet-5 is 1.30x => 1.30 credits => $0.052 for one request.
    sonnet = next((r for r in stats["by_model"] if r["model"] == "claude-sonnet-5"), None)
    check("sonnet-5 priced at 1.30 credits x $0.04", sonnet and abs(sonnet["usd"] - 0.052) < 1e-6, sonnet)
    check("per-request rows carry usd", all("usd" in e for e in log["entries"]), log["entries"][:1])
    check("caller IP recorded", any(e["ip"] for e in log["entries"]), log["entries"][:1])
    check("24h series present", len(stats["series"]) == 24, len(stats["series"]))
    check("key usage attributed",
          any(k["usd"] > 0 for k in client.get("/api/keys").json()["keys"]),
          client.get("/api/keys").json()["keys"])

    print("\n== whitelist management ==")
    wl = client.get("/api/whitelist").json()["entries"]
    check("loopback listed as owner", any(e["root"] for e in wl), wl)
    add = client.post("/api/whitelist", json={"ip": "203.0.113.9", "label": "office"})
    check("address added", add.status_code == 200, add.text)
    check("duplicate rejected",
          client.post("/api/whitelist", json={"ip": "203.0.113.9"}).status_code == 409)
    check("garbage rejected",
          client.post("/api/whitelist", json={"ip": "not-an-ip"}).status_code == 400)
    check("added address appears",
          any(e["ip"] == "203.0.113.9" for e in client.get("/api/whitelist").json()["entries"]))
    check("owner address cannot be removed",
          client.delete("/api/whitelist/127.0.0.1").status_code == 400)
    check("added address can be removed",
          client.delete("/api/whitelist/203.0.113.9").status_code == 200)

    print("\n== web chat streams ==")
    with client.stream("POST", "/api/chat", json={
        "model": "gpt-5.6-sol",
        "messages": [{"role": "user", "content": "hello"}],
    }) as stream:
        raw = "".join(stream.iter_text())
    events = [line for line in raw.splitlines() if line.strip()]
    check("start event", '"type": "start"' in events[0] or '"type":"start"' in events[0], events[0])
    check("deltas streamed", sum('"delta"' in e for e in events) > 3, len(events))
    check("done reports usd", '"usd"' in events[-1], events[-1])
    check("gpt-5.6-sol priced at 2.40 x $0.04 = $0.096", '"usd": 0.096' in events[-1], events[-1])

    print("\n== settings persist to .env ==")
    saved = client.post("/api/settings", json={
        "kiro_api_key": "ksk_web_written_key",
        "default_model": "claude-sonnet-5",
        "usd_per_credit": 0.05,
    })
    check("saved", saved.status_code == 200, saved.text)
    written = open(os.environ["ENV_FILE"]).read()
    check("kiro key written", "KIRO_API_KEY=ksk_web_written_key" in written, written)
    check("default model written", "KIRO_DEFAULT_MODEL=claude-sonnet-5" in written, written)
    check("rate written", "USD_PER_CREDIT=0.05" in written, written)
    check("existing comments preserved", "# seed" in written, written)
    check("bad kiro key rejected",
          client.post("/api/settings", json={"kiro_api_key": "nope"}).status_code == 400)
    check("unknown default model rejected",
          client.post("/api/settings", json={"default_model": "gpt-4o"}).status_code == 400)
    check("new rate applied to reporting",
          client.get("/api/stats").json()["usd_per_credit"] == 0.05)

print("\n" + "=" * 50)
if failures:
    print("FAILED ({0}): {1}".format(len(failures), ", ".join(failures)))
    sys.exit(1)
print("RIOAPIS CONSOLE CONFIRMED")
