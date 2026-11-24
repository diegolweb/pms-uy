// The folio's tax receipt where someone else issues it (Uruguay's CFE):
// kamra.localization.folio_fiscal_documents puts `fiscal_documents` on
// kamra.api.folio_invoice and the folio shows it in a card of its own -
// pending, in contingency, issued with its series and number, and the
// pack's notice when something is missing. The printed ZATCA block
// (`statutory`) stays as it was. The real FolioView runs in headless
// Chromium against a stubbed server:
//
//   node --test scripts/folio-fiscal-documents.test.mjs
//
// Needs this frontend's node_modules (esbuild comes with vite, playwright is
// a dev dependency) and playwright's Chromium. No network: every request the
// page makes is answered here.

import assert from "node:assert/strict"
import path from "node:path"
import { after, before, beforeEach, describe, test } from "node:test"
import { build } from "esbuild"
import { viteGlob } from "./esbuild-vite-glob.mjs"
import { chromium } from "playwright"

const SRC = path.resolve(import.meta.dirname, "../src")
const ORIGIN = "http://kamra.test"
const FOLIO = "FOLIO-T-0002"

const ENTRY = `
import { createRoot } from "react-dom/client"
import { MemoryRouter, Route, Routes } from "react-router-dom"
import { AuthProvider } from "./lib/auth"
import FolioView from "./screens/FolioView"
import { initLang } from "./lib/dir"

initLang().then(() => createRoot(document.getElementById("root")!).render(
  <MemoryRouter initialEntries={["/billing/${FOLIO}"]}>
    <AuthProvider>
      <Routes>
        <Route path="/billing/:name" element={<FolioView />} />
      </Routes>
    </AuthProvider>
  </MemoryRouter>,
))
`

let bundle
let browser

before(async () => {
  const out = await build({
    stdin: { contents: ENTRY, resolveDir: SRC, loader: "tsx", sourcefile: "entry.tsx" },
    bundle: true,
    write: false,
    format: "esm",
    jsx: "automatic",
    outdir: "out",
    logLevel: "silent",
    define: {
      "process.env.NODE_ENV": '"development"',
      "import.meta.env": JSON.stringify({ PROD: false, DEV: true, MODE: "test", BASE_URL: "/" }),
    },
    plugins: [viteGlob],
    loader: { ".png": "dataurl", ".svg": "dataurl", ".jpg": "dataurl", ".woff2": "dataurl" },
  })
  bundle = {
    js: out.outputFiles.find((f) => f.path.endsWith(".js")).text,
    css: out.outputFiles.find((f) => f.path.endsWith(".css"))?.text ?? "",
  }
  browser = await chromium.launch()
})

after(async () => {
  await browser?.close()
})

// --- what the Uruguay pack gives, in its three cases ---------------------------

const PENDING = {
  title: "Comprobante fiscal (CFE)",
  state: "contingency",
  text: "Comprobante fiscal pendiente: se emite cuando vuelva el proveedor de facturas.",
  notice: "",
  documents: [{ label: "e-Ticket", state: "contingency", number: null,
                text: "Comprobante fiscal pendiente: se emite cuando vuelva el proveedor de facturas.", url: null, date: null }],
  link: { label: "Facturas pendientes", url: "/facturas?property=Hotel%20T" },
}

const ISSUED = {
  title: "Comprobante fiscal (CFE)",
  state: "issued",
  text: "",
  notice: "",
  documents: [{ label: "e-Ticket", state: "issued", number: "Serie A, número 1234", text: "",
                url: "https://example.org/cfe/A-1234.pdf", date: "2026-12-07" }],
  link: null,
}

const NO_RESIDENCE = {
  title: "Comprobante fiscal (CFE)",
  state: "pending",
  text: "",
  notice: "Falta la residencia del huésped: indique en el check-in si reside en Uruguay y, si no, su país. " +
    "El comprobante fiscal sale en cuanto esté.",
  documents: [],
  link: { label: "Facturas pendientes", url: "/facturas?property=Hotel%20T" },
}

// --- the stubbed server -------------------------------------------------------

function invoice({ fiscal = null, statutory = null, settled = false } = {}) {
  return {
    folio: {
      name: FOLIO, property: "Hotel T", status: settled ? "Closed" : "Open", folio_type: "Guest",
      invoice_number: settled ? "INV-HT-26-00001" : null, guest_name: "Test Guest", charges: [],
      payments: [{ payment_kind: "Payment", posting_date: "2026-12-07", mode: "Cash", amount: 500, reference: null }],
      charges_total: 2000, tax_total: 0, grand_total: 2000, payments_total: 500, balance: 1500,
    },
    property: {
      name: "Hotel T", legal_name: null, logo_url: null, address: "1 Test St", state: null, gstin: null,
      phone: null, email: null, sac: null, place_of_supply: null, tax_id_label: "Tax ID", tax_note: "",
    },
    stay: { reservation: null, company: null },
    gst_summary: [],
    bill_to: null,
    statutory,
    fiscal_documents: fiscal,
    document: {
      title: settled ? "Invoice" : "Provisional Bill", title_local: null, is_final: settled,
      number: settled ? "INV-HT-26-00001" : FOLIO, date: "2026-12-07",
      amount_in_words: "", footer: null, service_code_label: null,
    },
    lines: [],
    guest: { name: "Test Guest", phone: null, email: "guest@example.com", nationality: null, address: "", id_type: null },
    summary_by_head: [],
    tax_summary: [],
  }
}

const LOCALE = {
  currency_symbol: "$", locale: "es-UY", currency: "UYU", tax_label: "IVA", tax_id_label: "RUT",
  tax_rates: [0, 10, 22], id_types: [], id_type_hints: {}, payment_modes: ["Cash"],
  instant_payment_modes: [], outlet_payment_modes: ["Cash"], default_nationality: "", adult_age: 18,
  minor_ids: "foreign", fnb_charge_words: [], desk_pages: [],
  integrations: { payment_gateways: [], tax_return: false, verify_tax_id: false, verify_guest_id: false },
}

class Server {
  constructor(bill) {
    this.bill = bill
    this.unknown = []
  }

  async answer(route) {
    const url = new URL(route.request().url())
    if (url.pathname === "/") {
      return route.fulfill({
        contentType: "text/html",
        body: `<!doctype html><html><head><style>${bundle.css}</style></head>` +
          `<body><div id="root"></div><script type="module" src="/app.js"></script></body></html>`,
      })
    }
    if (url.pathname === "/app.js") return route.fulfill({ contentType: "text/javascript", body: bundle.js })
    if (url.pathname.startsWith("/socket.io")) return route.abort()
    const method = url.pathname.replace("/api/method/", "")
    const reply = (message) => route.fulfill({ contentType: "application/json", body: JSON.stringify({ message }) })
    switch (method) {
      case "kamra.api.whoami":
        return reply({ user: "desk@example.com", full_name: "Desk", roles: ["Front Desk"] })
      case "kamra.api.folio_invoice":
        return reply(this.bill)
      case "kamra.api.property_locale":
        return reply(LOCALE)
      case "kamra.ledger.list_exchange_rates":
        return reply({ business_date: "2026-12-07", rates: [] })
      case "kamra.ledger.list_reason_codes":
        return reply([])
      case "kamra.api.allowance_requests":
        return reply({ requests: [], can_decide: false })
      case "kamra.payments.gateway_refunds":
        return reply({ gateway: null, payments: [] })
      default:
        this.unknown.push(method)
        return reply(null)
    }
  }
}

let pages = []
beforeEach(async () => {
  await Promise.all(pages.map((p) => p.context().close()))
  pages = []
})

async function opened(bill, { lang = "en" } = {}) {
  const server = new Server(bill)
  const context = await browser.newContext()
  await context.addInitScript((l) => {
    localStorage.setItem("kamra-lang", l)
    localStorage.setItem("kamra_property", "Hotel T")
  }, lang)
  const page = await context.newPage()
  pages.push(page)
  page.setDefaultTimeout(5000)
  page.errors = []
  page.on("pageerror", (e) => page.errors.push(String(e)))
  await page.route(`${ORIGIN}/**`, (route) => server.answer(route))
  await page.goto(`${ORIGIN}/`)
  if (process.env.DEBUG_FOLIO) {
    await page.waitForTimeout(2000)
    console.log("errors:", page.errors, "unknown:", server.unknown,
      "\nbody:", (await page.innerText("body")).slice(0, 1200))
  }
  await page.getByRole("heading", { name: /Payments|Pagos/ }).waitFor()
  page.server = server
  return page
}

const card = (page) => page.getByTestId("fiscal-documents")

// --- the tests ------------------------------------------------------------------

describe("The folio's tax receipt (CFE)", () => {
  test("pending: the state, no number yet, and the way to the pending invoices", async () => {
    const page = await opened(invoice({ fiscal: PENDING }))
    await card(page).waitFor()
    assert.equal(await card(page).getByRole("heading").innerText(), "Comprobante fiscal (CFE)")
    const [doc] = await card(page).getByTestId("fiscal-document").all()
    assert.match(await doc.innerText(), /e-Ticket/)
    assert.equal(await doc.locator("[data-state]").getAttribute("data-state"), "contingency")
    assert.match(await doc.innerText(), /In contingency/)
    assert.doesNotMatch(await doc.innerText(), /Serie A, número 1234/)
    const link = card(page).getByTestId("fiscal-documents-link")
    assert.equal(await link.innerText(), "Facturas pendientes")
    assert.equal(await link.getAttribute("href"), "/facturas?property=Hotel%20T")
    assert.equal(await card(page).getByRole("alert").count(), 0, "no notice when nothing is missing")
    assert.deepEqual(page.errors, [])
  })

  test("issued: its type, series and number, and its PDF", async () => {
    const page = await opened(invoice({ fiscal: ISSUED }))
    await card(page).waitFor()
    const [doc] = await card(page).getByTestId("fiscal-document").all()
    const text = await doc.innerText()
    assert.match(text, /e-Ticket/)
    assert.match(text, /Serie A, número 1234/)
    assert.match(text, /Issued/)
    assert.equal(await doc.getByRole("link", { name: "Open the PDF" }).getAttribute("href"),
      "https://example.org/cfe/A-1234.pdf")
    assert.equal(await card(page).getByTestId("fiscal-documents-link").count(), 0)
    assert.deepEqual(page.errors, [])
  })

  test("without the guest's residence: the notice says what to do", async () => {
    const page = await opened(invoice({ fiscal: NO_RESIDENCE }))
    await card(page).waitFor()
    assert.match(await card(page).getByRole("alert").innerText(), /^Falta la residencia del huésped/)
    assert.equal(await card(page).getByTestId("fiscal-document").count(), 0)
    assert.equal(await card(page).locator("[data-state]").first().getAttribute("data-state"), "pending")
    assert.equal(await card(page).getByTestId("fiscal-documents-link").getAttribute("href"),
      "/facturas?property=Hotel%20T")
  })

  test("the desk's words follow its language", async () => {
    const page = await opened(invoice({ fiscal: ISSUED }), { lang: "es" })
    await card(page).waitFor()
    assert.match(await card(page).innerText(), /Emitido/)
    assert.equal(await card(page).getByRole("link", { name: "Abrir el PDF" }).count(), 1)
  })

  test("a pack without it shows no card, and the screen never prints it", async () => {
    let page = await opened(invoice())
    await page.waitForTimeout(300)
    assert.equal(await card(page).count(), 0)

    page = await opened(invoice({ fiscal: ISSUED }))
    await card(page).waitFor()
    // this harness has no Tailwind build: the card carries the class that hides it in print
    assert.match(await card(page).getAttribute("class"), /(^|\s)print:hidden(\s|$)/,
      "the card is the desk's, not the bill's")
  })

  test("Saudi Arabia's printed ZATCA block is as it was, and is not the card", async () => {
    const statutory = { title: "Tax Invoice", title_ar: "فاتورة ضريبية", qr_image: "data:image/png;base64,iVBORw0KGgo=",
      uuid: "1f0c6b1e-0000-4000-8000-000000000001", icv: 7, status: "Reported" }
    const page = await opened(invoice({ statutory, settled: true }))
    await page.getByText("ZATCA · ICV 7").waitFor()
    assert.equal(await page.getByText("1f0c6b1e-0000-4000-8000-000000000001").count(), 1)
    assert.equal(await card(page).count(), 0)
  })
})
