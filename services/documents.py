"""
Document generation. Every generated PDF is populated from the real enrollment
record — no placeholder values when a real record exists (requirement #7).

Content is grounded in the actual Perch/Solstice NY contract templates the
team supplied (subscription agreement structure, CDG disclosure line items,
ESIGN/credit/phone consent language, NY DPS complaint line) — summarized and
restructured for this prototype's generated PDFs, not the verbatim licensed
text of those documents.
"""
import os
from datetime import datetime

from reportlab.lib.pagesizes import LETTER
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from pypdf import PdfWriter, PdfReader

STORAGE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "storage", "generated")
os.makedirs(STORAGE_DIR, exist_ok=True)

styles = getSampleStyleSheet()
H1 = ParagraphStyle("H1", parent=styles["Heading1"], fontSize=16, spaceAfter=10)
H2 = ParagraphStyle("H2", parent=styles["Heading2"], fontSize=12, spaceAfter=6, spaceBefore=12)
BODY = ParagraphStyle("Body", parent=styles["BodyText"], fontSize=9.5, leading=13.5)
SMALL = ParagraphStyle("Small", parent=styles["BodyText"], fontSize=8, textColor=colors.grey)


def _doc(path):
    return SimpleDocTemplate(path, pagesize=LETTER, topMargin=0.75 * inch, bottomMargin=0.75 * inch,
                              leftMargin=0.85 * inch, rightMargin=0.85 * inch)


def _kv_table(rows):
    t = Table(rows, colWidths=[1.7 * inch, 4.6 * inch])
    t.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor("#5C6E67")),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, colors.HexColor("#DFE7E2")),
    ]))
    return t


def generate_subscription_agreement(ctx, out_path):
    doc = _doc(out_path)
    story = [
        Paragraph("Community Solar Subscription Agreement", H1),
        Paragraph("Solstice Power Technologies LLC, a Perch Energy company (&ldquo;Service Provider&rdquo;)", SMALL),
        Spacer(1, 10),
        _kv_table([
            ["Subscriber", ctx["customer_name"]],
            ["Service address", ctx["service_address"]],
            ["Utility", ctx["utility"]],
            ["Utility account #", ctx["account_number_masked"]],
            ["Project", ctx["project_name"]],
            ["Enrollment ID", ctx["enrollment_code"]],
            ["Effective date", ctx["effective_date"]],
        ]),
        Spacer(1, 14),
        Paragraph("Key terms", H2),
        Paragraph(
            "Service Provider will assign Subscriber to an eligible community solar project and determine "
            "Subscriber's subscription size based on historical usage, not to exceed 100% of annual usage. "
            "Subscriber will receive monthly bill credits from " + ctx["utility"] + " based on their share of "
            "the project's generation, plus a statement of credits earned and savings realized.", BODY),
        Spacer(1, 6),
        Paragraph(
            "Initial term of one (1) year, automatically renewing for successive one-year terms unless either "
            "party provides written notice of non-renewal at least 90 days before the end of the then-current "
            "term. There is no fee for termination or non-renewal.", BODY),
        Spacer(1, 6),
        Paragraph(
            "Subscriber may terminate this Agreement by giving Service Provider written notice at least ninety "
            "(90) days before the desired termination date. Service Provider may terminate this Agreement at "
            "any time by giving Subscriber written notice that bill credits will no longer be applied.", BODY),
        Spacer(1, 6),
        Paragraph(
            "Subscriber authorizes Service Provider to obtain and review Subscriber's consumption history and "
            "billing determinants from the Utility for the purpose of administering this Agreement.", BODY),
    ]
    doc.build(story)
    return out_path


def generate_cdg_disclosure(ctx, out_path):
    doc = _doc(out_path)
    savings_pct = ctx["savings_pct"]
    example_savings = f"{savings_pct:.2f}"
    story = [
        Paragraph("Community Distributed Generation Disclosure Form", H1),
        Paragraph("Prepared by Solstice Power Technologies LLC, c/o Perch Community Solar, LLC", SMALL),
        Spacer(1, 10),
        _kv_table([
            ["Customer name", ctx["customer_name"]],
            ["Service address", ctx["service_address"]],
            ["Utility", ctx["utility"]],
            ["Enrollment ID", ctx["enrollment_code"]],
        ]),
        Spacer(1, 14),
        Paragraph("Subscription fee and savings rate", H2),
        Paragraph(
            f"Each month you will receive credit on your electric utility bill based on the electricity "
            f"generated by the project. After the subscription fee is deducted, you will receive savings "
            f"equal to <b>{savings_pct}%</b> of the value of the credits you receive each month. Example: "
            f"if your credits are $100 for a month, your savings for that month would be ${example_savings}. "
            f"You will not be charged any other fees.", BODY),
        Paragraph("Guarantees", H2),
        Paragraph(
            f"You are guaranteed to save money on your utility bill equal to {savings_pct}% of the credits "
            f"you receive. This does not guarantee your total utility bill will be higher or lower in any "
            f"particular month, and does not guarantee a minimum level of system production.", BODY),
        Paragraph("Right to cancel without penalty", H2),
        Paragraph(
            "You have the right to terminate this agreement without penalty within three (3) business days "
            "after signing by notifying the service provider via email at customercare@perchenergy.com or by "
            "phone at 1-888-893-3633.", BODY),
        Paragraph("Customer rights", H2),
        Paragraph(
            "For inquiries or complaints the provider is unable to resolve, contact the NY Department of "
            "Public Service Helpline at 1-800-342-3377, or file at dps.ny.gov/complaints.html.", BODY),
    ]
    doc.build(story)
    return out_path


def generate_income_survey(ctx, out_path):
    doc = _doc(out_path)
    lmi = ctx.get("lmi", {})
    if lmi.get("path") == "self_attestation":
        body = (
            f"Household size: {lmi.get('household_size')}. Applicable 80% State Median Income threshold: "
            f"${lmi.get('income_threshold'):,.0f}. Customer attested that household income is "
            f"<b>{lmi.get('attestation_response')}</b> this threshold, on {lmi.get('attestation_date')}."
        )
    elif lmi.get("path") == "document":
        body = (
            f"Customer provided documentation ({lmi.get('qualification_type') or 'a qualifying document'}) "
            f"in support of low-income program eligibility. Automated review result: "
            f"{lmi.get('review_result') or 'pending'}."
        )
    else:
        body = "No low-income program documentation was submitted for this enrollment."
    story = [
        Paragraph("Community Solar Subscriber Household Income Survey", H1),
        Spacer(1, 10),
        _kv_table([["Customer name", ctx["customer_name"]], ["Enrollment ID", ctx["enrollment_code"]]]),
        Spacer(1, 14),
        Paragraph(body, BODY),
        Spacer(1, 10),
        Paragraph(
            "This information is collected by Arcadia and shared with NYSERDA for program evaluation and "
            "incentive determination. It is not published or shared at the individual customer level.", SMALL),
    ]
    doc.build(story)
    return out_path


def generate_consent_doc(ctx, out_path, kind):
    doc = _doc(out_path)
    if kind == "esign_consent":
        title = "ESIGN Consent Disclosure"
        body = (
            "By signing electronically, you agree to receive required notices and disclosures electronically "
            "instead of on paper. You may request paper copies or withdraw this consent at any time by emailing "
            "customercare@perchenergy.com."
        )
    elif kind == "credit_contact_consent":
        title = "Credit Check &amp; Contact Consent"
        body = (
            "Credit check: you authorize a soft credit pull (does not affect your credit score) to help "
            "determine program eligibility.<br/><br/>Phone &amp; text: you authorize contact by phone, text, "
            "or automated dialing about this application. Consent is not required to purchase, and message/"
            "data rates may apply."
        )
    else:  # terms_privacy
        title = "Terms &amp; Conditions and Privacy Policy Acknowledgment"
        body = (
            "Covers acceptable use, intellectual property, disclaimers of warranty, limitation of liability, "
            "and how personal information is collected, used, and protected."
        )
    story = [
        Paragraph(title, H1),
        Spacer(1, 10),
        _kv_table([["Customer name", ctx["customer_name"]], ["Enrollment ID", ctx["enrollment_code"]]]),
        Spacer(1, 14),
        Paragraph(body, BODY),
    ]
    doc.build(story)
    return out_path


def generate_cover_sheet(ctx, out_path):
    doc = _doc(out_path)
    story = [
        Paragraph("Enrollment Cover Sheet", H1),
        Paragraph(f"Enrollment {ctx['enrollment_code']} — generated {datetime.now().strftime('%Y-%m-%d %H:%M')}", SMALL),
        Spacer(1, 14),
        Paragraph("Customer", H2),
        _kv_table([
            ["Name", ctx["customer_name"]], ["Email", ctx["customer_email"]], ["Phone", ctx["customer_phone"]],
            ["Service address", ctx["service_address"]],
        ]),
        Spacer(1, 10),
        Paragraph("Utility account", H2),
        _kv_table([
            ["Utility", ctx["utility"]], ["Account #", ctx["account_number_masked"]],
        ]),
        Spacer(1, 10),
        Paragraph("Project", H2),
        _kv_table([
            ["Project", ctx["project_name"]], ["Savings rate", f"{ctx['savings_pct']}%"],
        ]),
        Spacer(1, 10),
        Paragraph("Representative", H2),
        _kv_table([["Sales rep", ctx.get("rep_name", "—")]]),
    ]
    doc.build(story)
    return out_path


def generate_signature_certificate(ctx, signatures, out_path):
    doc = _doc(out_path)
    rows = [["Field", "Type", "Method", "Signer", "Completed at"]]
    for s in signatures:
        rows.append([s["field_key"], s["field_type"], s["method"] or "-", s["signer_name"], s["completed_at"]])
    t = Table(rows, colWidths=[1.6 * inch, 0.8 * inch, 0.8 * inch, 1.6 * inch, 1.5 * inch])
    t.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E7F1EC")),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#DFE7E2")),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
    ]))
    story = [
        Paragraph("Signature Certificate", H1),
        Paragraph(f"Enrollment {ctx['enrollment_code']}", SMALL),
        Spacer(1, 14),
        _kv_table([
            ["Signer", ctx["customer_name"]], ["Email", ctx["customer_email"]],
            ["Session completed", ctx.get("session_completed_at", "-")],
            ["IP address", ctx.get("ip_address", "-")],
        ]),
        Spacer(1, 14),
        Paragraph("Signature and initial events", H2),
        t,
    ]
    doc.build(story)
    return out_path


def merge_pdfs(paths, out_path):
    writer = PdfWriter()
    for p in paths:
        if p and os.path.exists(p):
            reader = PdfReader(p)
            for page in reader.pages:
                writer.add_page(page)
    with open(out_path, "wb") as f:
        writer.write(f)
    return out_path
