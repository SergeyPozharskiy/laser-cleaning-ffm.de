#!/usr/bin/env python3
"""Render an .eml file from a Jinja2 template, config.yaml and recipients.yaml.

Template path is read from config.yaml (template.path); override with --template.
Output dir is config.yaml (template.output_dir); relative --out is joined under it.
Output filename, if --out is omitted, is auto-derived from template + company name.

Example:
    python render_mail.py --recipient info@aventus-immo.de
    # -> intro/hausverwaltung_vorstellung-Aventus-Immobilien.eml
"""
from __future__ import annotations

import argparse
import sys
from email.utils import formataddr, formatdate
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined, TemplateError


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


def main() -> int:
    here = Path(__file__).parent
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--template", type=Path, default=None,
                   help="Path to .eml template (Jinja2). "
                        "Defaults to config.template.path.")
    p.add_argument("--recipient", required=True,
                   help="Recipient email — must exist in recipients.yaml")
    p.add_argument("--out", type=Path, default=None,
                   help="Output .eml path. If omitted, filename is auto-derived "
                        "as <template-stem-sans-tmpl_>-<company-with-dashes>.eml.")
    p.add_argument("--config", type=Path, default=here / "config.yaml")
    p.add_argument("--recipients", type=Path, default=here / "recipients.yaml")
    args = p.parse_args()

    config = load_yaml(args.config)
    recipients_doc = load_yaml(args.recipients)
    recipient = find_recipient(recipients_doc["recipients"], args.recipient)

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

    out_path = args.out or Path(derive_output_filename(template_path, recipient))
    if not out_path.is_absolute():
        output_dir_str = template_cfg.get("output_dir")
        if output_dir_str:
            output_dir = Path(output_dir_str)
            if not output_dir.is_absolute():
                output_dir = (args.config.parent / output_dir).resolve()
            out_path = output_dir / out_path

    sender = config["sender"]
    company = config["company"]

    # formataddr handles RFC 2047 encoding for non-ASCII display names (e.g. umlauts).
    from_header = formataddr((company["name"], sender["email"]))
    to_display = recipient.get("company_name") or ""
    to_header = formataddr((to_display, recipient["email"]))

    env = Environment(
        loader=FileSystemLoader(template_path.parent),
        undefined=StrictUndefined,   # missing variable => loud failure, not silent ""
        keep_trailing_newline=True,
        autoescape=False,            # this is an email body, not a web page
    )
    try:
        tmpl = env.get_template(template_path.name)
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
    except TemplateError as e:
        print(f"Template error: {e}", file=sys.stderr)
        return 1

    out_path.parent.mkdir(parents=True, exist_ok=True)
    # RFC 5322 mandates CRLF line endings.
    out_path.write_text(rendered, encoding="utf-8", newline="\r\n")
    print(f"Wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
