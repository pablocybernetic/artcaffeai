"""Shared, responsive presentation for transactional and team emails.

The body is an HTML fragment prepared by the caller; metadata is plain text.
This module formats messages only and never sends mail.
"""
from html import escape


def render_email(body: str, *, heading: str = "Artcaffe", category: str = "Notification", preheader: str = "") -> str:
    if 'data-artcaffe-email="1"' in body:
        return body
    title = escape(heading)
    label = escape(category)
    preview = escape(preheader or heading)
    return f'''<!doctype html>
<html lang="en" data-artcaffe-email="1">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  body {{ margin:0; padding:0; -webkit-text-size-adjust:100%; }}
  table {{ border-spacing:0; }}
  .email-content {{ overflow-wrap:anywhere; word-wrap:break-word; }}
  .email-content p {{ margin:0 0 12px; }}
  .email-content a {{ color:#087f3b; word-break:break-word; }}
  .email-content img {{ max-width:100%; height:auto; }}
  .email-content table {{ max-width:100%; table-layout:fixed; }}
  .email-content td, .email-content th {{ overflow-wrap:anywhere; word-wrap:break-word; }}
  @media only screen and (max-width:480px) {{
    .email-outer {{ padding:12px 8px !important; }}
    .email-pad {{ padding:18px 16px !important; }}
    .email-content a[style*="inline-block"] {{ display:block !important; margin:8px 0 !important; text-align:center !important; }}
  }}
</style>
</head>
<body style="margin:0;padding:0;background:#f3f4f4;color:#222725;font-family:Arial,Helvetica,sans-serif;">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;font-size:1px;line-height:1px;">{preview}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="width:100%;background:#f3f4f4;">
<tr><td align="center" class="email-outer" style="padding:24px 12px;">
<!--[if mso]><table role="presentation" width="600" cellpadding="0" cellspacing="0"><tr><td><![endif]-->
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="width:100%;max-width:600px;table-layout:fixed;border:1px solid #e2e7e3;border-top:4px solid #087f3b;border-radius:16px;background:#ffffff;overflow:hidden;">
<tr><td class="email-pad" style="padding:20px 24px;border-bottom:1px solid #e6ebe7;background:#f2f8f3;overflow-wrap:anywhere;word-wrap:break-word;">
<p style="margin:0 0 6px;color:#087f3b;font-size:10px;font-weight:700;line-height:16px;letter-spacing:1.3px;text-transform:uppercase;">{label}</p>
<p style="margin:0;font-size:20px;line-height:28px;font-weight:700;color:#202822;">{title}</p>
</td></tr>
<tr><td class="email-pad email-content" style="padding:20px 24px;font-size:14px;line-height:1.65;color:#37423b;overflow-wrap:anywhere;word-wrap:break-word;">{body}</td></tr>
<tr><td style="padding:14px 24px;border-top:1px solid #e6ebe7;font-size:11px;line-height:18px;color:#717b74;">Artcaffe &nbsp;·&nbsp; {label}</td></tr>
</table>
<!--[if mso]></td></tr></table><![endif]-->
</td></tr></table>
</body></html>'''
