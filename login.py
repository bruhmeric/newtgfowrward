"""Standalone Telethon login helper.

Run this ONCE before starting the bot:

    python login.py            # file-based session (default, for local dev)
    python login.py --string   # StringSession (recommended for Render)
    python login.py --verify   # check an existing SESSION_STRING (no login)

File-based mode:
  * Saves the session as <SESSION_NAME>.session
  * Use this when running the bot locally with python bot.py

String mode (--string flag):
  * Uses Telethon's StringSession (no file written)
  * Prints the session string at the end
  * Copy that string into Render's SESSION_STRING environment variable
  * The same string works on any server — no file system required

Verify mode (--verify flag):
  * Reads SESSION_STRING from the environment (.env or export)
  * Checks it parses, connects, and the account is still authorized
  * Exits 0 when the string is good, 1 when it is not
  * Use this to test a string BEFORE pasting it into Render

In login modes, you'll be prompted for:
  * A confirmation code sent to your Telegram
  * (Optional) 2FA password if your account has it
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from telethon import TelegramClient
from telethon.sessions import StringSession


async def verify_session_string(api_id: int, api_hash: str) -> None:
    """Validate SESSION_STRING end-to-end: parse -> connect -> authorized.

    Mirrors exactly what the bot does at startup (via UserSession), so if
    this prints OK, Render will connect too. Exits 1 with a concrete reason
    on any failure.
    """
    from user_session import UserSession

    session_string = os.environ.get("SESSION_STRING", "").strip()
    if not session_string:
        print("ERROR: SESSION_STRING is empty.")
        print("Generate one first:  python login.py --string")
        sys.exit(1)

    print(f"SESSION_STRING length: {len(session_string)} chars, "
          f"prefix: {session_string[:1]!r} (must be '1')")

    us = UserSession("verify", api_id, api_hash, session_string=session_string)
    if us._init_error:
        print(f"FAIL: {us._init_error}")
        sys.exit(1)
    print(f"Parsed OK — connecting to Telegram ({UserSession.CONNECT_TIMEOUT}s timeout)...")

    ok = await us.start()
    try:
        await us.stop()
    except Exception:
        pass
    if not ok:
        print(f"FAIL: {us.last_error}")
        print("\nFix: generate a fresh string and update Render:")
        print("  1. python login.py --string")
        print("  2. Render -> your service -> Environment -> SESSION_STRING -> paste")
        print("  3. Restart/redeploy the service")
        sys.exit(1)

    print(f"OK — session is valid and authorized as {us.connected_as}.")
    print("Safe to paste into Render's SESSION_STRING.")
    sys.exit(0)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Telethon login helper")
    parser.add_argument(
        "--string", action="store_true",
        help="Use StringSession and print it after login (recommended for Render)"
    )
    parser.add_argument(
        "--verify", action="store_true",
        help="Check the existing SESSION_STRING env var: does it parse, connect, "
             "and is the account authorized? No login prompts."
    )
    args = parser.parse_args()

    api_id_raw = os.environ.get("API_ID", "").strip()
    api_hash = os.environ.get("API_HASH", "").strip()
    phone = os.environ.get("PHONE", "").strip()
    session_name = os.environ.get("SESSION_NAME", "user_session").strip() or "user_session"

    if not api_id_raw or not api_hash:
        print("ERROR: API_ID and API_HASH must be set in .env")
        print("Get them from https://my.telegram.org/apps -> API development tools")
        sys.exit(1)

    try:
        api_id = int(api_id_raw)
    except ValueError:
        print(f"ERROR: API_ID must be an integer, got {api_id_raw!r}")
        sys.exit(1)

    if args.verify:
        return await verify_session_string(api_id, api_hash)

    if not phone:
        phone_input = input("Enter your phone number (with country code, e.g. +15551234567): ").strip()
        if not phone_input:
            print("ERROR: phone number is required")
            sys.exit(1)
        phone = phone_input

    if args.string:
        print("Connecting with StringSession...")
        client = TelegramClient(StringSession(), api_id, api_hash)
    else:
        print(f"Connecting with file-based session '{session_name}'...")
        client = TelegramClient(session_name, api_id, api_hash)

    await client.start(phone=phone)

    me = await client.get_me()
    print("\n=== Login successful ===")
    print(f"  User: @{getattr(me, 'username', None)} (id={me.id})")
    print(f"  Name: {getattr(me, 'first_name', '')} {getattr(me, 'last_name', '')}")

    if args.string:
        session_string = client.session.save()
        # Show WHICH datacenter the string embeds — if it was generated behind
        # a VPN/proxy it may contain an address that will never work from a
        # public host like Render. Catch that HERE, before the string ever
        # leaves this machine.
        try:
            from user_session import classify_session_address
            parsed = StringSession(session_string)
            kind = classify_session_address(parsed.server_address or "")
            print(f"  Session DC:  DC{parsed.dc_id} ({parsed.server_address}:{parsed.port})")
            if kind == "private":
                print()
                print("  ⚠️  WARNING: this session embeds a PRIVATE address "
                      f"({parsed.server_address}).")
                print("  It was generated behind a VPN/proxy and will NEVER work")
                print("  on a public host like Render. Disconnect the VPN/proxy and")
                print("  re-run:  python login.py --string")
            elif kind == "public":
                print()
                print("  ⚠️  NOTE: this address is not a known Telegram DC IP —")
                print("  if the bot can't connect on Render, regenerate the string")
                print("  with:  python login.py --string")
        except Exception:
            pass  # diagnostics must never block printing the string itself
        print()
        print("=" * 70)
        print("  SESSION_STRING (copy this ENTIRE line, including '1' prefix):")
        print()
        print(f"  {session_string}")
        print()
        print("=" * 70)
        print()
        print("  Next steps for Render:")
        print("    1. Copy the SESSION_STRING above")
        print("    2. In Render, go to your service -> Environment -> Add Environment Variable")
        print("    3. Key: SESSION_STRING  Value: (paste the string)")
        print("    4. Save and restart the service")
        print()
        print("  ⚠️  Treat this string like a password — it grants full")
        print("  access to your Telegram account.")
        print()
        print("  IMPORTANT: generate this on a NORMAL connection (no VPN/proxy)")
        print("  and with the SAME API_ID/API_HASH you set on Render — the")
        print("  session is bound to both.")
        print()
        print("  Verify BEFORE pasting into Render:")
        print("    SESSION_STRING=<string> API_ID=... API_HASH=... python login.py --verify")
    else:
        print(f"\n  Session file: {session_name}.session")
        print("\n  You can now run `python bot.py` — it will reuse this session "
              "without prompting again.")

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
