#!/usr/bin/env python3
"""Render an .eml file from a Jinja2 template, config.yaml and recipients.yaml.

Template path is read from config.yaml (template.path); override with --template.
Recipients path is read from config.yaml (recipients.path); override with
    --recipients. Falls back to recipients.yaml next to the config.
Output dir is config.yaml (template.output_dir); relative --out is joined under it.
Output filename, if --out is omitted, is auto-derived from template + company name.

Examples:
    python render_mail.py --recipient info@aventus-immo.de
    # -> intro/ready/hausverwaltung_vorstellung-Aventus-Immobilien.eml

    python render_mail.py --all-pending
    # renders one .eml per recipient whose status == 'pending'
"""
from __future__ import annotations

import argparse
import sys
from email.utils import formataddr, formatdate
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined, Template, TemplateError


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
) -> None:
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
    args = p.parse_args()

    if args.all_pending and args.out is not None:
        raise SystemExit("--out cannot be combined with --all-pending")

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

    env = Environment(
        loader=FileSystemLoader(template_path.parent),
        undefined=StrictUndefined,   # missing variable => loud failure, not silent ""
        keep_trailing_newline=True,
        autoescape=False,            # this is an email body, not a web page
    )
    try:
        tmpl = env.get_template(template_path.name)
        for recipient in targets:
            out_path = resolve_out_path(args.out, template_path, recipient, output_dir)
            render_one(tmpl, sender, company, recipient, out_path)
    except TemplateError as e:
        print(f"Template error: {e}", file=sys.stderr)
        return 1

    if args.all_pending:
        print(f"Rendered {len(targets)} email(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
