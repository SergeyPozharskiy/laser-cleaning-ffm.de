#!/usr/bin/env python3
"""Render (and optionally send) .eml files from a Jinja2 template,
config.yaml and recipients.yaml.

Template path is read from config.yaml (template.path); override with --template.
Recipients path is read from config.yaml (recipients.path); override with
    --recipients. Falls back to recipients.yaml next to the config.
Output dir is config.yaml (template.output_dir); relative --out is joined under it.
Output filename, if --out is omitted, is auto-derived from template + company name.

Without --send the script only renders .eml files to disk (open them in a mail
client and send manually). With --send the script also delivers each message
via SMTP (settings under `smtp:` in config.yaml, password from env
SMTP_PASSWORD or .env file), updates the recipient's `status` / `sent_at` in
the recipients YAML, and paces sends with a configurable interval + jitter and
a daily limit (see `send:` in config.yaml).

Examples:
    python render_mail.py --recipient info@aventus-immo.de
    # -> intro/ready/hausverwaltung_vorstellung-Aventus-Immobilien.eml

    python render_mail.py --all-pending
    # renders one .eml per recipient whose status == 'pending'

    python render_mail.py --all-pending --send
    # renders AND sends, respecting interval + daily_limit, updates YAML

    python render_mail.py --all-pending --send --dry-run
    # like --send but does not actually connect to SMTP or touch YAML
"""
from __future__ import annotations

import argparse
import json
import os
import random
import smtplib
import ssl
import sys
import time
from datetime import datetime, timezone
from email import message_from_string
from email.utils import formataddr, formatdate
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined, Template, TemplateError
from ruamel.yaml import YAML


def load_yaml(path: Path) -> dict:
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def find_recipient(recipients: list[dict], email: str) -> dict:
    needle = email.lower()
    for r in recipients:
        if (r.get("email") or "").lower() == needle:
            return r
    raise SystemExit(f"Recipient {email!r} not found in recipients file")


def derive_output_filename(template_path: Path, recipient: dict) -> str:
    """Build "<template-stem-without-tmpl_>-<company-with-dashes>.eml"."""
    stem = template_path.stem
    if stem.startswith("tmpl_"):
        stem = stem[len("tmpl_"):]
    company = recipient.get("company_name") or recipient["email"]
    company = company.strip().replace(" ", "-")
    return f"{stem}-{company}{template_path.suffix or '.eml'}"


def resolve_out_path(
    explicit: Path | None,
    template_path: Path,
    recipient: dict,
    output_dir: Path | None,
) -> Path:
    out_path = explicit or Path(derive_output_filename(template_path, recipient))
    if not out_path.is_absolute() and output_dir is not None:
        out_path = output_dir / out_path
    return out_path


def render_one(
    tmpl: Template,
    sender: dict,
    company: dict,
    recipient: dict,
    out_path: Path,
) -> str:
    """Render the template, write it to disk, and return the rendered text."""
    from_header = formataddr((company["name"], sender["email"]))
    to_display = recipient.get("company_name") or ""
    to_header = formataddr((to_display, recipient["email"]))

    rendered = tmpl.render(
        sender=sender,
        company=company,
        recipient=recipient,
        headers={
            "from": from_header,
            "to": to_header,
            "date": formatdate(localtime=True),
        },
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    # RFC 5322 mandates CRLF line endings.
    out_path.write_text(rendered, encoding="utf-8", newline="\r\n")
    print(f"Wrote {out_path}")
    return rendered


# ---------- SMTP send ------------------------------------------------------

def load_dotenv(path: Path) -> None:
    """Tiny `.env` loader — KEY=VALUE per line, ignores blanks and `#` lines.
    Does not override variables already set in the real environment."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def connect_smtp(smtp_cfg: dict, password: str) -> smtplib.SMTP:
    host = smtp_cfg["host"]
    port = int(smtp_cfg["port"])
    security = (smtp_cfg.get("security") or "starttls").lower()
    username = smtp_cfg["username"]

    if security == "ssl":
        client = smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=30)
    else:
        client = smtplib.SMTP(host, port, timeout=30)
        client.ehlo()
        if security == "starttls":
            client.starttls(context=ssl.create_default_context())
            client.ehlo()
    client.login(username, password)
    return client


def send_via_smtp(
    client: smtplib.SMTP,
    rendered: str,
    from_address: str,
    to_address: str,
) -> None:
    msg = message_from_string(rendered)
    client.send_message(msg, from_addr=from_address, to_addrs=[to_address])


def _yaml_rt() -> YAML:
    """Ruamel.yaml round-trip parser configured to match the on-disk style
    of recipients*.yaml: 2-space mapping indent, list items indented 2 under
    their key, quotes preserved, no line reflow, explicit `null` keyword."""
    y = YAML()
    y.preserve_quotes = True
    y.width = 4096
    y.indent(mapping=2, sequence=4, offset=2)
    y.representer.add_representer(
        type(None),
        lambda r, _d: r.represent_scalar("tag:yaml.org,2002:null", "null"),
    )
    return y


def mark_recipient_sent(recipients_path: Path, email: str) -> None:
    """Update status=sent, sent_at=<ISO now> for `email` in the recipients YAML.
    Uses ruamel.yaml so comments and key order in the file are preserved."""
    yaml_rt = _yaml_rt()
    with recipients_path.open("r", encoding="utf-8") as f:
        doc = yaml_rt.load(f)

    needle = email.lower()
    now_iso = datetime.now().astimezone().isoformat(timespec="seconds")
    for entry in doc["recipients"]:
        if (entry.get("email") or "").lower() == needle:
            entry["status"] = "sent"
            entry["sent_at"] = now_iso
            break
    else:
        raise RuntimeError(f"mark_recipient_sent: {email!r} not in {recipients_path}")

    # newline="" → preserve the LF line endings ruamel.yaml emits, instead of
    # letting Python translate to CRLF on Windows (the source file is LF).
    with recipients_path.open("w", encoding="utf-8", newline="") as f:
        yaml_rt.dump(doc, f)


# ---------- Daily-limit state ---------------------------------------------

class DailyCounter:
    """Per-UTC-date send counter persisted to a small JSON file."""

    def __init__(self, state_path: Path, limit: int) -> None:
        self.path = state_path
        self.limit = limit
        self.today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.count = 0
        if state_path.is_file():
            try:
                data = json.loads(state_path.read_text(encoding="utf-8"))
                if data.get("date") == self.today:
                    self.count = int(data.get("count") or 0)
            except (json.JSONDecodeError, ValueError):
                pass

    def remaining(self) -> int:
        return max(0, self.limit - self.count)

    def reached(self) -> bool:
        return self.count >= self.limit

    def increment(self) -> None:
        self.count += 1
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"date": self.today, "count": self.count}),
            encoding="utf-8",
        )


def sleep_with_jitter(interval: float, jitter_pct: float) -> None:
    if interval <= 0:
        return
    factor = 1.0 + random.uniform(-jitter_pct / 100.0, jitter_pct / 100.0)
    delay = max(0.0, interval * factor)
    print(f"  …sleeping {delay:.1f}s before next send")
    time.sleep(delay)


# ---------- main -----------------------------------------------------------

def main() -> int:
    here = Path(__file__).parent
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--template", type=Path, default=None,
                   help="Path to .eml template (Jinja2). "
                        "Defaults to config.template.path.")
    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument("--recipient",
                        help="Recipient email — must exist in recipients.yaml")
    target.add_argument("--all-pending", action="store_true",
                        help="Render an .eml for every recipient whose "
                             "status == 'pending'.")
    p.add_argument("--out", type=Path, default=None,
                   help="Output .eml path (single-recipient mode only). "
                        "If omitted, filename is auto-derived as "
                        "<template-stem-sans-tmpl_>-<company-with-dashes>.eml.")
    p.add_argument("--config", type=Path, default=here / "config.yaml")
    p.add_argument("--recipients", type=Path, default=None,
                   help="Path to recipients .yaml. "
                        "Defaults to config.recipients.path, then to "
                        "'recipients.yaml' next to the config file.")
    p.add_argument("--send", action="store_true",
                   help="After rendering, actually send each message over SMTP "
                        "(see `smtp:` and `send:` in config.yaml). Updates "
                        "status=sent / sent_at in the recipients YAML.")
    p.add_argument("--dry-run", action="store_true",
                   help="With --send: print what would be sent but skip the "
                        "actual SMTP connection and YAML status update. "
                        "No effect without --send.")
    args = p.parse_args()

    if args.all_pending and args.out is not None:
        raise SystemExit("--out cannot be combined with --all-pending")
    if args.dry_run and not args.send:
        raise SystemExit("--dry-run only makes sense together with --send")

    config = load_yaml(args.config)

    recipients_cfg = config.get("recipients") or {}
    recipients_path = args.recipients
    if recipients_path is None:
        recipients_path = Path(recipients_cfg.get("path") or "recipients.yaml")
    if not recipients_path.is_absolute():
        recipients_path = (args.config.parent / recipients_path).resolve()

    recipients_doc = load_yaml(recipients_path)
    all_recipients = recipients_doc["recipients"]

    if args.all_pending:
        targets = [r for r in all_recipients if (r.get("status") or "").lower() == "pending"]
        if not targets:
            print("No recipients with status 'pending' — nothing to do.")
            return 0
    else:
        targets = [find_recipient(all_recipients, args.recipient)]

    template_cfg = config.get("template") or {}

    template_path = args.template
    if template_path is None:
        if not template_cfg.get("path"):
            raise SystemExit(
                "No --template given and config.yaml has no template.path"
            )
        template_path = Path(template_cfg["path"])
    if not template_path.is_absolute():
        template_path = (args.config.parent / template_path).resolve()

    output_dir: Path | None = None
    output_dir_str = template_cfg.get("output_dir")
    if output_dir_str:
        output_dir = Path(output_dir_str)
        if not output_dir.is_absolute():
            output_dir = (args.config.parent / output_dir).resolve()

    sender = config["sender"]
    company = config["company"]

    # ----- SMTP / send-mode setup --------------------------------------
    smtp_client: smtplib.SMTP | None = None
    counter: DailyCounter | None = None
    send_cfg: dict = config.get("send") or {}
    smtp_cfg: dict = config.get("smtp") or {}
    from_address = ""
    if args.send:
        if not smtp_cfg:
            raise SystemExit("--send requires an `smtp:` section in config.yaml")
        load_dotenv(args.config.parent / ".env")
        password = os.environ.get("SMTP_PASSWORD") or ""
        if not args.dry_run and not password:
            raise SystemExit(
                "--send requires SMTP_PASSWORD in env (or in .env next to "
                "config.yaml). Copy .env.example to .env and fill it in."
            )
        from_address = smtp_cfg.get("from_address") or smtp_cfg.get("username") or sender["email"]

        state_file = Path(send_cfg.get("state_file") or ".send_state.json")
        if not state_file.is_absolute():
            state_file = (args.config.parent / state_file).resolve()
        daily_limit = int(send_cfg.get("daily_limit") or 0)
        counter = DailyCounter(state_file, daily_limit) if daily_limit > 0 else None

        if counter is not None and counter.reached():
            print(
                f"Daily limit reached ({counter.count}/{counter.limit}) for "
                f"{counter.today}. Try again tomorrow, or raise send.daily_limit."
            )
            return 0

        if not args.dry_run:
            print(f"Connecting to {smtp_cfg['host']}:{smtp_cfg['port']} as {smtp_cfg['username']}…")
            smtp_client = connect_smtp(smtp_cfg, password)

    # ----- Render (and send) loop --------------------------------------
    env = Environment(
        loader=FileSystemLoader(template_path.parent),
        undefined=StrictUndefined,   # missing variable => loud failure, not silent ""
        keep_trailing_newline=True,
        autoescape=False,            # this is an email body, not a web page
    )

    interval = float(send_cfg.get("interval_seconds") or 0)
    jitter_pct = float(send_cfg.get("jitter_pct") or 0)
    sent_count = 0
    stopped_early = False

    try:
        tmpl = env.get_template(template_path.name)
        for i, recipient in enumerate(targets):
            if args.send and counter is not None and counter.reached():
                print(
                    f"Daily limit hit ({counter.count}/{counter.limit}). "
                    f"Stopping. Re-run tomorrow to continue."
                )
                stopped_early = True
                break

            out_path = resolve_out_path(args.out, template_path, recipient, output_dir)
            rendered = render_one(tmpl, sender, company, recipient, out_path)

            if args.send:
                to_addr = recipient["email"]
                if args.dry_run:
                    print(f"  [dry-run] would send to {to_addr}")
                else:
                    if i > 0:
                        sleep_with_jitter(interval, jitter_pct)
                    assert smtp_client is not None
                    send_via_smtp(smtp_client, rendered, from_address, to_addr)
                    print(f"  Sent to {to_addr}")
                    mark_recipient_sent(recipients_path, to_addr)
                    if counter is not None:
                        counter.increment()
                sent_count += 1
    except TemplateError as e:
        print(f"Template error: {e}", file=sys.stderr)
        return 1
    finally:
        if smtp_client is not None:
            try:
                smtp_client.quit()
            except smtplib.SMTPException:
                pass

    if args.all_pending:
        print(f"Rendered {len(targets) if not stopped_early else sent_count} email(s).")
    if args.send and not args.dry_run:
        suffix = ""
        if counter is not None:
            suffix = f" — {counter.count}/{counter.limit} used today"
        print(f"Sent {sent_count} email(s){suffix}.")
    elif args.send and args.dry_run:
        print(f"[dry-run] Would have sent {sent_count} email(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
