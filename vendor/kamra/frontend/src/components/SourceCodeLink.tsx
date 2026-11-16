import { Fragment, useEffect, useState } from "react"
import { useT } from "../lib/i18n"
import { getSiteInfo, type SiteInfo } from "../lib/siteInfo"

/**
 * AGPL §13: the offer of the corresponding source, on every screen people use
 * over the network. One link per app from site_info (Kamra first, labelled
 * "Source code"; any other app by its name). Inherits the size and colour of
 * the text it sits next to; renders nothing until site_info says where.
 */
export function SourceCodeLink({ className }: { className?: string }) {
  const { t } = useT()
  const [links, setLinks] = useState<NonNullable<SiteInfo["source_code"]>>([])

  useEffect(() => {
    let live = true
    getSiteInfo().then((info) => {
      if (live) setLinks(info.source_code ?? [])
    })
    return () => {
      live = false
    }
  }, [])

  if (!links.length) return null
  return (
    <span data-testid="source-code-link" className={className}>
      {links.map((link, i) => (
        <Fragment key={link.app}>
          {i > 0 ? " · " : null}
          <a href={link.url} target="_blank" rel="noopener noreferrer" className="hover:underline">
            {link.app === "kamra" ? t("Source code") : link.app}
          </a>
        </Fragment>
      ))}
    </span>
  )
}
