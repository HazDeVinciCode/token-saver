"""Faux `claude` pour les tests du runner `nuit` : imite `claude -p … --output-format json`.

Scénario par appel, lu dans le fichier FAKE_CLAUDE_SCRIPT (une ligne par appel) : ok | auth | limit | error | model.
"""
import json
import os
import sys
import time


def main() -> int:
    argv = sys.argv[1:]
    prompt = argv[argv.index("-p") + 1] if "-p" in argv else ""
    script = os.environ.get("FAKE_CLAUDE_SCRIPT")
    lines = open(script, encoding="utf-8").read().splitlines() if script and os.path.exists(script) else []
    counter = os.environ.get("FAKE_CLAUDE_COUNTER", script + ".n")
    n = int(open(counter).read()) if os.path.exists(counter) else 0
    open(counter, "w").write(str(n + 1))
    kind = lines[n] if n < len(lines) else "ok"
    calls = os.environ.get("FAKE_CLAUDE_CALLS", script + ".calls")
    with open(calls, "a", encoding="utf-8") as f:
        f.write(json.dumps({"argv": argv, "cwd": os.getcwd(), "env_has_app_vars": any(k.startswith("CLAUDE_CODE_SESSION") for k in os.environ),
                            "stdin_tty": None}) + "\n")
    base = {"type": "result", "session_id": f"fake-{n}", "num_turns": 12, "duration_ms": 1500, "total_cost_usd": 0.42,
            "usage": {"input_tokens": 100, "cache_read_input_tokens": 900000, "cache_creation_input_tokens": 50000, "output_tokens": 8000}}
    if kind == "auth":
        base.update({"subtype": "success", "is_error": True, "result": "Not logged in · Please run /login"}); code = 1
    elif kind == "limit":
        base.update({"subtype": "success", "is_error": True, "result": "You've hit your session limit · resets 11:59pm (Europe/Paris)"}); code = 1
    elif kind == "model":
        if "--model" in argv:
            base.update({"subtype": "success", "is_error": True, "result": "Unknown model variant"}); code = 1
        else:
            base.update({"subtype": "success", "is_error": False, "result": "cycle fait sans variante"}); code = 0
    elif kind == "error":
        base.update({"subtype": "error_during_execution", "is_error": True, "result": "boom"}); code = 1
    elif kind == "wait":
        base.update({"subtype": "success", "is_error": False, "result": "J'attends le verdict de la campagne."}); code = 0
    else:
        base.update({"subtype": "success", "is_error": False, "result": f"cycle fait ({prompt[:20]})"}); code = 0
    time.sleep(0.05)
    print(json.dumps(base))
    return code


if __name__ == "__main__":
    sys.exit(main())
