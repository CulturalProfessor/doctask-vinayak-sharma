/**
 * The pieces a reviewer actually looks at.
 *
 * The design rule throughout: **show the evidence next to the decision.** A
 * reviewer asked to approve "hourly_rate: 2 distinct values" is not reviewing
 * anything. A reviewer shown USD 120 from invoice_1043.txt at characters
 * 484-491, quoted, beside USD 135 from amendment_01.md, is.
 *
 * The second rule: **never let the UI imply something is settled when it is
 * not.** A proposed conflict resolution is rendered as a suggestion with its
 * reasoning, under a line saying nothing has been applied, because that is what
 * it is everywhere else in the system.
 */
import { useState } from 'react'

export function Span({ citation }) {
  return (
    <div className="span">
      <div className="span-source">
        <span className="doc">{citation.document}</span>
        {citation.doc_type && <span className="pill">{citation.doc_type}</span>}
        <span className="offsets">
          [{citation.char_start}–{citation.char_end}]
        </span>
      </div>
      <blockquote>{citation.quote}</blockquote>
      {citation.value && <div className="span-value">{citation.value}</div>}
    </div>
  )
}

function Verdict({ value, onChange }) {
  return (
    <div className="verdict">
      <button
        className={value === true ? 'approve on' : 'approve'}
        onClick={() => onChange(value === true ? null : true)}
      >
        Approve
      </button>
      <button
        className={value === false ? 'reject on' : 'reject'}
        onClick={() => onChange(value === false ? null : false)}
      >
        Reject
      </button>
    </div>
  )
}

function Decided({ proposal }) {
  return (
    <div className={`decided ${proposal.status}`}>
      {proposal.status === 'approved' ? 'Approved' : 'Rejected'}
      {proposal.decided_by && <> by {proposal.decided_by}</>}
      {proposal.decided_via && <> via {proposal.decided_via}</>}
      {proposal.reason && <div className="reason">“{proposal.reason}”</div>}
    </div>
  )
}

function ConflictBody({ payload }) {
  return (
    <>
      <div className="values">
        {payload.values.map((value) => (
          <span key={value} className="value">{value}</span>
        ))}
      </div>
      <div className="citations">
        {payload.members.map((member, i) => (
          <Span key={i} citation={member} />
        ))}
      </div>
      <div className="proposal-note">
        {payload.proposed ? (
          <>
            <strong>Proposed:</strong> {payload.proposed}
            {payload.proposed_from && <> from {payload.proposed_from}</>}
          </>
        ) : (
          <><strong>Proposed:</strong> none — the documents do not settle this.</>
        )}
        <div className="rationale">{payload.rationale}</div>
        <div className="unresolved">
          Status: open. This is a proposal for review. No value has been resolved
          or applied.
        </div>
      </div>
    </>
  )
}

function FindingBody({ payload }) {
  return (
    <>
      <div className="statement">{payload.statement}</div>
      <div className="detail">{payload.detail}</div>
      {payload.citations.length > 0 ? (
        <div className="citations">
          {payload.citations.map((citation, i) => (
            <Span key={i} citation={citation} />
          ))}
        </div>
      ) : (
        <div className="rationale">
          No fact citations: a quarantined document never produced one, by
          design. The document itself is the evidence.
        </div>
      )}
    </>
  )
}

function SectionBody({ payload }) {
  return (
    <>
      <div className="hash">content hash {payload.content_hash.slice(0, 16)}…</div>
      <pre className="section-body">{payload.body}</pre>
    </>
  )
}

export function Proposal({ proposal, verdict, onVerdict }) {
  const [open, setOpen] = useState(proposal.kind !== 'section_patch')
  const payload = proposal.payload
  const pending = proposal.status === 'pending'

  const label =
    proposal.kind === 'conflict'
      ? payload.field
      : proposal.kind === 'finding'
        ? payload.rule_key
        : payload.section_key

  return (
    <article className={`proposal ${proposal.kind} ${pending ? '' : 'settled'}`}>
      <header onClick={() => setOpen(!open)}>
        <span className={`kind ${proposal.kind}`}>
          {proposal.kind === 'section_patch' ? 'section' : proposal.kind}
        </span>
        <span className="label">{label}</span>
        {payload.severity && (
          <span className={`severity ${payload.severity}`}>{payload.severity}</span>
        )}
        <span className="chev">{open ? '▾' : '▸'}</span>
      </header>

      <div className="summary">{proposal.summary}</div>

      {open && (
        <div className="body">
          {proposal.kind === 'conflict' && <ConflictBody payload={payload} />}
          {proposal.kind === 'finding' && <FindingBody payload={payload} />}
          {proposal.kind === 'section_patch' && <SectionBody payload={payload} />}
        </div>
      )}

      {pending ? (
        <Verdict value={verdict} onChange={onVerdict} />
      ) : (
        <Decided proposal={proposal} />
      )}
    </article>
  )
}

export function Stages({ report }) {
  if (!report) return null
  const total = report.totals
  return (
    <div className="stages">
      <table>
        <thead>
          <tr>
            <th>Stage</th><th>Entries</th><th>ms</th>
            <th>Tokens in</th><th>Tokens out</th><th>Cost</th><th>Paths taken</th>
          </tr>
        </thead>
        <tbody>
          {report.stages.map((row) => (
            <tr key={row.stage}>
              <td className="stage">{row.stage}</td>
              <td className="num">{row.calls}</td>
              <td className="num">{row.ms ?? 0}</td>
              <td className="num">{row.tokens_in}</td>
              <td className="num">{row.tokens_out}</td>
              <td className="num">${Number(row.cost_usd).toFixed(4)}</td>
              <td>
                {row.paths.map((path) => (
                  <span key={path} className="path">{path}</span>
                ))}
              </td>
            </tr>
          ))}
        </tbody>
        <tfoot>
          <tr>
            <td>total</td><td /><td className="num">{total.ms}</td>
            <td className="num">{total.tokens_in}</td>
            <td className="num">{total.tokens_out}</td>
            <td className="num">${Number(total.cost_usd).toFixed(4)}</td>
            <td />
          </tr>
        </tfoot>
      </table>
    </div>
  )
}

export function Findings({ findings }) {
  if (!findings) return null
  const order = { violated: 0, not_enough_evidence: 1, satisfied: 2 }
  const rows = [...findings.findings].sort(
    (a, b) => order[a.outcome] - order[b.outcome] || a.rule_key.localeCompare(b.rule_key),
  )
  return (
    <div className="findings">
      <p className="note">
        Every rule's answer, not only the broken ones. <em>Not enough evidence</em>{' '}
        is not <em>satisfied</em>: a rule the pile could not answer is not a rule
        the pile passed.
      </p>
      {rows.map((finding) => (
        <div key={finding.id} className={`finding ${finding.outcome}`}>
          <div className="finding-head">
            <span className={`outcome ${finding.outcome}`}>
              {finding.outcome.replace(/_/g, ' ')}
            </span>
            <span className="rule">{finding.rule_key}</span>
            <span className={`severity ${finding.severity}`}>{finding.severity}</span>
          </div>
          <div className="detail">{finding.detail}</div>
          {finding.citations.length > 0 && (
            <div className="citations">
              {finding.citations.map((citation, i) => (
                <Span key={i} citation={citation} />
              ))}
            </div>
          )}
        </div>
      ))}
    </div>
  )
}

export function Register({ register }) {
  if (!register) return null
  return (
    <div className="register">
      {register.sections.map((section) => (
        <div key={section.section_key} className="section">
          <div className="section-head">
            <span className="key">{section.section_key}</span>
            <span className="hash">{section.content_hash.slice(0, 16)}…</span>
          </div>
          <pre className="section-body">{section.body}</pre>
        </div>
      ))}
    </div>
  )
}

export function Audit({ audit }) {
  if (!audit?.audit?.length) return null
  return (
    <table className="audit">
      <thead>
        <tr>
          <th>Section</th><th>From</th><th>To</th><th>Because of</th><th>When</th>
        </tr>
      </thead>
      <tbody>
        {audit.audit.map((row, i) => (
          <tr key={i}>
            <td>{row.section_key}</td>
            <td className="mono">{row.from_hash ? row.from_hash.slice(0, 10) : '—'}</td>
            <td className="mono">{row.to_hash.slice(0, 10)}</td>
            <td>{row.cause_document ?? '—'}</td>
            <td className="when">{new Date(row.committed_at).toLocaleString()}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}
