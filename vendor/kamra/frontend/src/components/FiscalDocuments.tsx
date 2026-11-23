import { ExternalLink, FileText } from "lucide-react"
import { Badge } from "./ui/badge"
import { useT } from "../lib/i18n"

/** The folio's tax receipt where someone else issues it (Uruguay's CFE):
 *  kamra.localization.folio_fiscal_documents. The pack words its own text;
 *  the state badges are Kamra's. Never the printed ZATCA block. */
export type FiscalState = "pending" | "contingency" | "issued" | "attention"

export interface FiscalDocumentsData {
  title: string
  state: FiscalState
  text: string
  notice: string
  documents: {
    label: string
    state: FiscalState
    number: string | null
    text: string
    url: string | null
    date: string | null
  }[]
  link: { label: string; url: string } | null
}

const TONE = { pending: "amber", contingency: "amber", issued: "green", attention: "rose" } as const

function StateBadge({ state }: { state: FiscalState }) {
  const { t } = useT()
  const label = {
    pending: t("Pending"),
    contingency: t("In contingency"),
    issued: t("Issued"),
    attention: t("Needs attention"),
  }[state]
  return <Badge tone={TONE[state]} data-state={state}>{label}</Badge>
}

export default function FiscalDocuments({ data }: { data: FiscalDocumentsData }) {
  const { t } = useT()
  return (
    <section
      data-testid="fiscal-documents"
      aria-label={data.title || t("Tax receipt")}
      className="mb-4 rounded-lg border border-zinc-200 bg-white px-4 py-3 text-sm print:hidden"
    >
      <div className="flex flex-wrap items-center gap-2">
        <FileText className="size-4 shrink-0 text-zinc-500" aria-hidden />
        <h2 className="font-medium text-zinc-800">{data.title || t("Tax receipt")}</h2>
        <StateBadge state={data.state} />
        {data.link && (
          <a
            href={data.link.url}
            target="_blank"
            rel="noreferrer"
            data-testid="fiscal-documents-link"
            className="ml-auto inline-flex items-center gap-1 text-xs font-medium text-brand-700 hover:underline"
          >
            {data.link.label}
            <ExternalLink className="size-3" aria-hidden />
          </a>
        )}
      </div>
      {data.notice && (
        <p role="alert" data-testid="fiscal-documents-notice"
          className="mt-2 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-amber-800">
          {data.notice}
        </p>
      )}
      {data.text && !data.notice && <p className="mt-1 text-zinc-600">{data.text}</p>}
      {data.documents.length > 0 && (
        <ul className="mt-2 divide-y divide-zinc-100">
          {data.documents.map((doc, i) => (
            <li key={i} data-testid="fiscal-document" className="flex flex-wrap items-center gap-x-3 gap-y-1 py-1.5">
              <span className="font-medium text-zinc-700">{doc.label}</span>
              {doc.number && <span className="font-mono text-zinc-800">{doc.number}</span>}
              <StateBadge state={doc.state} />
              {doc.date && <span className="text-xs text-zinc-500">{doc.date}</span>}
              {doc.text && <span className="basis-full text-xs text-zinc-500">{doc.text}</span>}
              {doc.url && (
                <a href={doc.url} target="_blank" rel="noreferrer"
                  className="inline-flex items-center gap-1 text-xs font-medium text-brand-700 hover:underline">
                  {t("Open the PDF")}
                  <ExternalLink className="size-3" aria-hidden />
                </a>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
