import { useEffect, type ReactNode } from "react";
import { Link, useParams } from "react-router-dom";

/** Public pages (no sign-in). Bracketed items are for the operator's legal team: nothing here invents them. */
const DRAFT = "Draft. The bracketed items and the legal wording need your data protection officer's review before this goes live.";

const PAGES: Record<string, { title: string; body: ReactNode }> = {
  privacy: {
    title: "Privacy notice",
    body: (
      <>
        <h2>Who is responsible</h2>
        <p>[Company name, address], the controller of your data. Data protection officer: [name, contact].</p>
        <h2>What we process, and why</h2>
        <ul>
          <li><strong>Support emails you send us:</strong> to answer you and fix the problem you report (Art. 6(1)(b) GDPR, [confirm legal basis]).</li>
          <li><strong>Sign-in data of our staff:</strong> to protect the console (Art. 6(1)(f) GDPR, [confirm legal basis]).</li>
        </ul>
        <h2>How AI is used</h2>
        <p>An AI model running on our own systems helps classify emails and draft replies. It sees text in which names, addresses, phone numbers and bank details are replaced by tokens. A named member of staff approves every ticket, production action and customer message before it happens.</p>
        <h2>How long we keep it</h2>
        <ul>
          <li>Raw emails: 30 days.</li>
          <li>Real names and addresses (the token vault): [confirm; designed as 30 days after last use, with no open case].</li>
          <li>Other pseudonymised case records: [agree with client].</li>
          <li>Audit log, with tokens only: [per client policy].</li>
        </ul>
        <h2>Your rights</h2>
        <p>You can ask for access to your data and a copy of it, and for correction, erasure or restriction, and you can object to processing (Art. 15 to 21 GDPR). We answer within one month. Contact: [privacy contact address]. You can also complain to a supervisory authority: [authority].</p>
        <h2>Where it is held</h2>
        <p>[Confirm: EU data region.] Tickets are kept in [Jira Cloud region] and contain tokens, not names.</p>
      </>
    ),
  },
  imprint: {
    title: "Imprint",
    body: (
      <>
        <p>[Company name and legal form]<br />[Address]<br />[Representative]</p>
        <p>Contact: [email, phone]<br />Register: [court, number]<br />VAT ID: [number]</p>
      </>
    ),
  },
  accessibility: {
    title: "Accessibility statement",
    body: (
      <>
        <p>We want this console to be usable by everyone. It is designed with keyboard use, visible focus, reduced motion and light and dark themes in mind.</p>
        <h2>Status</h2>
        <p>No formal accessibility audit has been done yet, so we do not claim conformity with a standard such as WCAG 2.2 AA or EN 301 549. [Update after the audit.]</p>
        <h2>Feedback</h2>
        <p>If something does not work for you, write to [accessibility contact]. Enforcement body: [name].</p>
      </>
    ),
  },
};

export function LegalPage() {
  const { page = "" } = useParams();
  const content = PAGES[page];
  const title = content?.title ?? "Page not found";
  useEffect(() => { document.title = `${title} · OpsPilot`; }, [title]);
  return (
    <main style={{ maxWidth: 720, margin: "0 auto", padding: "40px 24px", lineHeight: 1.55, background: "var(--bg)", color: "var(--ink)" }}>
      <Link to="/" style={{ fontSize: 14 }}>← OpsPilot</Link>
      <h1 style={{ fontSize: 28, fontWeight: 600, margin: "16px 0 8px" }}>{title}</h1>
      {content ? (
        <>
          <div role="note" className="alert alert-warn" style={{ margin: "12px 0 20px" }}>{DRAFT}</div>
          {content.body}
        </>
      ) : <p>There is no such page.</p>}
    </main>
  );
}
