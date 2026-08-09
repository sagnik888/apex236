"""Complete the Upstox OAuth flow and persist an access token.

Thin CLI over `upstox_auth`. The lifecycle logic — expiry, verification,
atomic caching, api_key binding — lives there so the CLI, the FastAPI
endpoints and the dispatcher can never disagree about the session.

    python upstox_login.py                 # interactive
    python upstox_login.py "<code|url>"    # non-interactive (scriptable)
    python upstox_login.py --check         # report state, change nothing
    python upstox_login.py --url           # print the login URL and exit

Upstox tokens expire daily at 03:30 IST and cannot be renewed programmatically
(the exchange step needs a human browser login), so this runs once per trading
day. The account password is never used: Upstox has no password-based API login.
"""
from __future__ import annotations

import sys
from datetime import datetime

from upstox_auth import IST, get_upstox_auth, reset_upstox_auth


def _print_status(auth) -> int:
    st = auth.status()
    print("=" * 64)
    print("UPSTOX SESSION STATE")
    print("=" * 64)
    missing = st["missing_credentials"]
    print(f"credentials     : {'OK' if not missing else 'MISSING ' + ', '.join(missing)}")
    print(f"connected       : {st['connected']}")
    if st["user_id"]:
        print(f"user            : {st['user_id']}")
    if st["expires_at"]:
        exp = datetime.fromisoformat(st["expires_at"])
        print(f"expires         : {exp:%Y-%m-%d %H:%M:%S IST}  ({st['hours_remaining']} h left)")
    print(f"daily cutoff    : {st['daily_cutoff_ist']} IST")
    if st["expiring_soon"]:
        print("                  ** EXPIRING WITHIN THE HOUR - re-authenticate now **")
    if st["auth_required"]:
        print(f"ACTION REQUIRED : {st['reason']}")
    return 0 if st["connected"] else 1


def main(argv: list[str]) -> int:
    auth = get_upstox_auth()

    if "--check" in argv:
        return _print_status(auth)

    if "--url" in argv:
        try:
            print(auth.login_url())
            return 0
        except ValueError as exc:
            print(f"Error: {exc}")
            return 1

    positional = [a for a in argv if not a.startswith("-")]
    code = positional[0] if positional else ""

    if not code:
        try:
            url = auth.login_url()
        except ValueError as exc:
            print(f"Error: {exc}")
            return 1
        print("=" * 64)
        print("1. Open this URL and log in:")
        print()
        print(f"   {url}")
        print()
        print("2. You are redirected to <redirect_uri>?code=XXXXXXX")
        print("   Paste the FULL URL (or just the code) below.")
        print("   Select the WHOLE address-bar value — a truncated code is the")
        print("   most common failure, and the code expires in ~2 minutes.")
        print("=" * 64)
        try:
            code = input("\ncode or redirect URL: ")
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            return 1

    extracted = auth.extract_code(code)
    if not extracted:
        print("No code supplied.")
        return 1
    if len(extracted) < 8:
        print(f"Warning: the code is only {len(extracted)} characters ({extracted!r}).")
        print("That is short for an Upstox authorization code — it looks truncated.")

    print("\nExchanging code for an access token ...")
    try:
        result = auth.complete_login(extracted)
    except Exception as exc:
        print(f"FAILED: {exc}")
        print()
        print("Most common causes:")
        print("  * the code was truncated, already used, or expired (~2 minutes)")
        print("  * UPSTOX_REDIRECT_URI does not EXACTLY match the app registration")
        print("  * the API key/secret belong to a different app than you logged into")
        return 1

    print("Success. Token verified and cached.")
    if result.get("expires_at"):
        exp = datetime.fromisoformat(result["expires_at"])
        print(f"  expires {exp:%Y-%m-%d %H:%M:%S IST}")
    print("\nRestart the scanner, then confirm with:")
    print("  python upstox_login.py --check")
    reset_upstox_auth()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
