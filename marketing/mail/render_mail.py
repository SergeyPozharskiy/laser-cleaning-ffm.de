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
import imaplib
import json
import os
import random
import smtplib
import ssl
import sys
import time
from datetime import datetime, timezone
from email import message_from_bytes
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
    # Parse from BYTES, not str: the template body is 8-bit UTF-8 (ä ö ü ß …).
    # message_from_bytes decodes non-ASCII via surrogateescape so the BytesGenerator
    # inside send_message can round-trip it back to the original UTF-8 bytes. Parsing
    # from a str would keep genuine code points and raise UnicodeEncodeError on send.
    msg = message_from_bytes(rendered.encode("utf-8"))
    client.send_message(msg, from_addr=from_address, to_addrs=[to_address])


def _is_disconnect(exc: Exception) -> bool:
    """True for errors that mean 'the session died', not 'the mail was rejected'.
    IONOS reports an idle-timed-out session as a 421 on the next command, which
    smtplib surfaces as SMTPSenderRefused(421, …) rather than a disconnect."""
    if isinstance(exc, (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError)):
        return True
    if isinstance(exc, smtplib.SMTPResponseException):
        return exc.smtp_code in (421, 451)
    return isinstance(exc, OSError)


# ---------- IMAP: save a copy to the Sent folder ---------------------------

def connect_imap(imap_cfg: dict, password: str) -> imaplib.IMAP4:
    host = imap_cfg["host"]
    port = int(imap_cfg.get("port") or 993)
    security = (imap_cfg.get("security") or "ssl").lower()
    username = imap_cfg["username"]

    if security == "ssl":
        client: imaplib.IMAP4 = imaplib.IMAP4_SSL(
            host, port, ssl_context=ssl.create_default_context()
        )
    else:
        client = imaplib.IMAP4(host, port)
        if security == "starttls":
            client.starttls(ssl.create_default_context())
    client.login(username, password)
    return client


def _imap_quote_mailbox(name: str) -> str:
    """Wrap a mailbox name in an IMAP quoted-string so names containing spaces
    (e.g. "Gesendete Objekte") are not misparsed as two arguments. imaplib does
    not quote mailbox names itself."""
    escaped = name.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def append_to_sent(client: imaplib.IMAP4, rendered: str, sent_folder: str) -> None:
    """APPEND the rendered message to the IMAP Sent folder, flagged \\Seen so
    it does not show up as unread. Line endings are normalised to CRLF as
    RFC 3501/5322 require."""
    crlf = rendered.replace("\r\n", "\n").replace("\n", "\r\n")
    msg_bytes = crlf.encode("utf-8")
    date_time = imaplib.Time2Internaldate(time.time())
    typ, data = client.append(
        _imap_quote_mailbox(sent_folder), r"(\Seen)", date_time, msg_bytes
    )
    if typ != "OK":
        raise imaplib.IMAP4.error(f"APPEND to {sent_folder!r} failed: {typ} {data!r}")


# ---------- connection management -----------------------------------------

class Mailer:
    """Lazily-opened SMTP + optional IMAP connections.

    A single connection cannot be held across the pacing interval: IONOS closes
    sessions that sit idle for ~2 minutes and answers the next command with
    `421 … command timeout`. So `close()` is called before every sleep and the
    connections are re-opened on demand for the next message."""

    def __init__(
        self,
        smtp_cfg: dict,
        imap_cfg: dict,
        password: str,
        imap_password: str,
        sent_folder: str,
    ) -> None:
        self.smtp_cfg = smtp_cfg
        self.imap_cfg = imap_cfg
        self.password = password
        self.imap_password = imap_password
        self.sent_folder = sent_folder
        self._smtp: smtplib.SMTP | None = None
        self._imap: imaplib.IMAP4 | None = None

    def smtp(self) -> smtplib.SMTP:
        if self._smtp is None:
            print(
                f"Connecting to {self.smtp_cfg['host']}:{self.smtp_cfg['port']} "
                f"as {self.smtp_cfg['username']}…"
            )
            self._smtp = connect_smtp(self.smtp_cfg, self.password)
        return self._smtp

    def imap(self) -> imaplib.IMAP4 | None:
        if not self.imap_cfg:
            return None
        if self._imap is None:
            print(
                f"Connecting to IMAP {self.imap_cfg['host']}:"
                f"{self.imap_cfg.get('port') or 993} to save copies in "
                f"{self.sent_folder!r}…"
            )
            try:
                self._imap = connect_imap(self.imap_cfg, self.imap_password)
            except (imaplib.IMAP4.error, OSError, ssl.SSLError) as e:
                print(
                    f"  ! IMAP connection failed ({e}). Continuing without "
                    f"saving a copy to {self.sent_folder!r}.",
                    file=sys.stderr,
                )
                self._imap = None
        return self._imap

    def send(self, rendered: str, from_address: str, to_address: str) -> None:
        """Send, and on a dead session reconnect once and try again."""
        try:
            send_via_smtp(self.smtp(), rendered, from_address, to_address)
        except (smtplib.SMTPException, OSError) as e:
            if not _is_disconnect(e):
                raise
            print(f"  ! SMTP session lost ({e}). Reconnecting…", file=sys.stderr)
            self._close_smtp()
            send_via_smtp(self.smtp(), rendered, from_address, to_address)

    def save_copy(self, rendered: str) -> None:
        client = self.imap()
        if client is None:
            return
        try:
            append_to_sent(client, rendered, self.sent_folder)
        except (imaplib.IMAP4.error, OSError, ssl.SSLError) as e:
            print(
                f"  ! Could not save copy to {self.sent_folder!r} ({e}). "
                f"Mail was still sent.",
                file=sys.stderr,
            )
            self._close_imap()
            return
        print(f"  Saved copy to {self.sent_folder!r}")

    def _close_smtp(self) -> None:
        if self._smtp is not None:
            try:
                self._smtp.quit()
            except (smtplib.SMTPException, OSError):
                pass
            self._smtp = None

    def _close_imap(self) -> None:
        if self._imap is not None:
            try:
                self._imap.logout()
            except (imaplib.IMAP4.error, OSError, ssl.SSLError):
                pass
            self._imap = None

    def close(self) -> None:
        self._close_smtp()
        self._close_imap()


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
    mailer: Mailer | None = None
    counter: DailyCounter | None = None
    send_cfg: dict = config.get("send") or {}
    smtp_cfg: dict = config.get("smtp") or {}
    imap_cfg: dict = config.get("imap") or {}
    sent_folder = imap_cfg.get("sent_folder") or "Sent"
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
            # Connections are opened on first use and dropped again before each
            # pacing sleep — see Mailer.
            mailer = Mailer(
                smtp_cfg,
                imap_cfg,
                password,
                os.environ.get("IMAP_PASSWORD") or password,
                sent_folder,
            )

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
                    assert mailer is not None
                    if i > 0:
                        # Hang up first: the server drops idle sessions during
                        # the pause and answers the next command with 421.
                        mailer.close()
                        sleep_with_jitter(interval, jitter_pct)
                    mailer.send(rendered, from_address, to_addr)
                    print(f"  Sent to {to_addr}")
                    mailer.save_copy(rendered)
                    mark_recipient_sent(recipients_path, to_addr)
                    if counter is not None:
                        counter.increment()
                sent_count += 1
    except TemplateError as e:
        print(f"Template error: {e}", file=sys.stderr)
        return 1
    finally:
        if mailer is not None:
            mailer.close()

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
