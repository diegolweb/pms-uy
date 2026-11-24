// The folio's gateway refund: "Refund" on a payment the property's gateway
// posted asks the gateway to give (part of) it back with
// kamra.payments.refund_via_gateway. The real FolioView runs in headless
// Chromium against a stubbed server, so what is tested is what the desk sees
// and what the screen sends:
//
//   node --test scripts/folio-gateway-refund.test.mjs
//
// Needs this frontend's node_modules (esbuild comes with vite, playwright is
// a dev dependency) and playwright's Chromium (npx playwright install
// chromium). No network: every request the page makes is answered here.

import assert from "node:assert/strict"
import path from "node:path"
import { after, before, beforeEach, describe, test } from "node:test"
import { build } from "esbuild"
import { viteGlob } from "./esbuild-vite-glob.mjs"
import { chromium } from "playwright"

const SRC = path.resolve(import.meta.dirname, "../src")
const ORIGIN = "http://kamra.test"
const FOLIO = "FOLIO-T-0001"
const GATEWAY_REF = "pay-777"

const ENTRY = `
import { createRoot } from "react-dom/client"
import { MemoryRouter, Route, Routes } from "react-router-dom"
import { AuthProvider } from "./lib/auth"
import FolioView from "./screens/FolioView"
import { initLang } from "./lib/dir"

// Kamra 2.7.10 loads a language's dictionary on demand (lib/locales.ts): as
// main.tsx does, have it in memory before the first paint (HR-183).
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

// --- the stubbed server -------------------------------------------------------

function invoice(payments, status = "Open") {
  const paid = payments.reduce((s, p) => s + p.amount, 0)
  return {
    folio: {
      name: FOLIO, property: "Hotel T", status, folio_type: "Guest", invoice_number: null,
      guest_name: "Test Guest", charges: [], payments,
      charges_total: 2000, tax_total: 0, grand_total: 2000, payments_total: paid, balance: 2000 - paid,
    },
    property: {
      name: "Hotel T", legal_name: null, logo_url: null, address: "1 Test St", state: null, gstin: null,
      phone: null, email: null, sac: null, place_of_supply: null, tax_id_label: "Tax ID", tax_note: "",
    },
    stay: { reservation: null, company: null },
    gst_summary: [],
    bill_to: null,
    statutory: null,
    document: {
      title: "Provisional Bill", title_local: null, is_final: false, number: FOLIO, date: "2026-11-19",
      amount_in_words: "", footer: null, service_code_label: null,
    },
    lines: [],
    guest: { name: "Test Guest", phone: null, email: "guest@example.com", nationality: null, address: "", id_type: null },
    summary_by_head: [],
    tax_summary: [],
  }
}

// cash with no reference, a desk card payment with a typed slip number, a
// payment the gateway posted, and a refund the gateway already reported
const PAYMENTS = [
  { payment_kind: "Payment", posting_date: "2026-11-18", mode: "Cash", amount: 300, reference: null },
  { payment_kind: "Payment", posting_date: "2026-11-18", mode: "Card", amount: 200, reference: "4321" },
  { payment_kind: "Advance", posting_date: "2026-11-18", mode: "Card", amount: 1250.5, reference: GATEWAY_REF },
  { payment_kind: "Refund", posting_date: "2026-11-19", mode: "Card", amount: -100, reference: `refund:${GATEWAY_REF}:r1` },
]

const LOCALE = {
  currency_symbol: "$", locale: "en-US", currency: "USD", tax_label: "Tax", tax_id_label: "Tax ID",
  tax_rates: [0, 10], id_types: [], id_type_hints: {}, payment_modes: ["Cash", "Card", "Bank Transfer"],
  instant_payment_modes: [], outlet_payment_modes: ["Cash", "Card"], default_nationality: "", adult_age: 18,
  minor_ids: "foreign", fnb_charge_words: [],
  integrations: { payment_gateways: ["Example Pay"], tax_return: false, verify_tax_id: false, verify_guest_id: false },
}

class Server {
  constructor() {
    this.roles = ["Front Desk"]
    this.payments = PAYMENTS.map((p) => ({ ...p }))
    this.status = "Open"
    // what kamra.payments.gateway_refunds answers
    this.refundable = { gateway: "Example Pay", payments: [{ reference: GATEWAY_REF, amount: 1250.5, left: 1150.5 }] }
    this.refundAsks = []
    this.refundableAsks = 0
    this.unknown = []
    // the answer to each refund_via_gateway, in order; the last one repeats
    this.refundAnswers = [{ status: "pending", message: "Refund asked of the gateway." }]
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
    if (url.pathname.startsWith("/socket.io")) return route.abort()  // no socket: the screen polls
    const method = url.pathname.replace("/api/method/", "")
    const params = JSON.parse(route.request().postData() || "{}")
    const reply = (message) => route.fulfill({ contentType: "application/json", body: JSON.stringify({ message }) })
    switch (method) {
      case "kamra.api.whoami":
        return reply({ user: "desk@example.com", full_name: "Desk", roles: this.roles })
      case "kamra.api.folio_invoice":
        return reply(invoice(this.payments, this.status))
      case "kamra.api.property_locale":
        return reply(LOCALE)
      case "kamra.languages.enabled_ui_languages":  // Kamra 2.8 (#630): the languages the site offers
        return reply({ enabled: ["en", "es", "pt"] })
      case "kamra.ledger.list_exchange_rates":
        return reply({ business_date: "2026-11-19", rates: [] })
      case "kamra.ledger.list_reason_codes":
        return reply([])
      case "kamra.api.allowance_requests":
        return reply({ requests: [], can_decide: false })
      case "kamra.api.linked_records":
        return reply({
          guest: null, guest_name: null, reservations: [], folios: [], company: null,
          group_booking: null, group_name: null, event: null, event_type: null,
        })
      case "kamra.payments.gateway_refunds":
        this.refundableAsks++
        return reply(this.refundable)
      case "kamra.payments.refund_via_gateway": {
        this.refundAsks.push(params)
        const answer = this.refundAnswers[Math.min(this.refundAsks.length - 1, this.refundAnswers.length - 1)]
        if (answer.delay) await new Promise((r) => setTimeout(r, answer.delay))
        if (answer.abort) return route.abort("connectionreset")
        if (answer.error) {
          return route.fulfill({
            status: 417, contentType: "application/json",
            body: JSON.stringify({
              exc_type: "ValidationError",
              _server_messages: JSON.stringify([JSON.stringify({ message: answer.error })]),
            }),
          })
        }
        return reply({ status: answer.status, message: answer.message })
      }
      default:
        this.unknown.push(method)
        return reply(null)
    }
  }
}

async function openFolio(server, { lang = "en" } = {}) {
  const context = await browser.newContext()
  await context.addInitScript((l) => {
    localStorage.setItem("kamra-lang", l)
    localStorage.setItem("kamra_property", "Hotel T")
  }, lang)
  const page = await context.newPage()
  page.setDefaultTimeout(5000)
  page.errors = []
  page.on("pageerror", (e) => page.errors.push(String(e)))
  await page.route(`${ORIGIN}/**`, (route) => server.answer(route))
  page.on("console", (m) => process.env.DEBUG_FOLIO && console.log("console:", m.type(), m.text()))
  await page.goto(`${ORIGIN}/`)
  if (process.env.DEBUG_FOLIO) {
    await page.waitForTimeout(2000)
    console.log("errors:", page.errors, "\nbody:", (await page.content()).slice(0, 1500))
  }
  await page.getByRole("heading", { name: /Payments|Pagos/ }).waitFor()
  return page
}

const row = (page, text) =>
  page.getByRole("listitem").filter({ hasText: text }).filter({ hasNotText: "refund:" })

async function startRefund(page) {
  await row(page, GATEWAY_REF).getByRole("button", { name: "Refund", exact: true }).click()
  return {
    amount: page.getByRole("spinbutton", { name: "Amount" }),
    reason: page.getByRole("textbox", { name: "Refund reason" }),
    submit: page.getByRole("button", { name: /^Refund \$/ }),
  }
}

let pages = []
beforeEach(async () => {
  await Promise.all(pages.map((p) => p.context().close()))
  pages = []
})
async function opened(server, opts) {
  const page = await openFolio(server, opts)
  pages.push(page)
  return page
}

// --- the tests ------------------------------------------------------------------

describe("Refund through the gateway on the folio", () => {
  test("only the payment the gateway posted has a Refund button", async () => {
    const server = new Server()
    const page = await opened(server)
    await row(page, GATEWAY_REF).getByRole("button", { name: "Refund", exact: true }).waitFor()
    assert.equal(await page.getByRole("button", { name: "Refund", exact: true }).count(), 1,
      "one Refund button on the folio")
    assert.equal(await row(page, "4321").getByRole("button").count(), 0,
      "a desk payment with a typed reference has none")
    assert.equal(await row(page, "Cash").getByRole("button").count(), 0, "cash has none")
    assert.equal(await page.getByRole("listitem").filter({ hasText: "refund:" }).getByRole("button").count(), 0,
      "a refund row has none")
    assert.deepEqual(server.unknown, [])
    assert.deepEqual(page.errors, [])
  })

  test("no button when the gateway gives nothing back or the folio is closed", async () => {
    const none = new Server()
    none.refundable = { gateway: "Example Pay", payments: [] }
    let page = await opened(none)
    await page.waitForFunction(() => document.body.innerText.includes("pay-777"))
    await page.waitForTimeout(300)
    assert.equal(await page.getByRole("button", { name: "Refund", exact: true }).count(), 0)

    const closed = new Server()
    closed.status = "Closed"
    page = await opened(closed)
    await page.waitForTimeout(300)
    assert.equal(await page.getByRole("button", { name: "Refund", exact: true }).count(), 0)
    assert.equal(closed.refundableAsks, 0, "a closed folio does not ask")

    const housekeeping = new Server()
    housekeeping.roles = ["Housekeeping"]
    page = await opened(housekeeping)
    await page.waitForTimeout(300)
    assert.equal(await page.getByRole("button", { name: "Refund", exact: true }).count(), 0)
    assert.equal(housekeeping.refundableAsks, 0, "who cannot refund does not ask")
  })

  test("an amount above what is left of the payment is refused", async () => {
    const server = new Server()
    const page = await opened(server)
    const form = await startRefund(page)
    assert.equal(await form.amount.inputValue(), "1150.5", "starts on what is left")
    await form.reason.fill("Guest cancelled")
    await form.amount.fill("1200")
    assert.equal(await form.submit.isDisabled(), true)
    assert.equal(await form.amount.getAttribute("aria-invalid"), "true")
    await form.submit.click({ force: true })
    await form.amount.fill("0")
    assert.equal(await form.submit.isDisabled(), true, "nothing to give back")
    await page.waitForTimeout(200)
    assert.equal(server.refundAsks.length, 0, "nothing asked")

    // what is left is fine; the server's own refusal reads as it says it
    server.refundAnswers = [{ error: "Only $50.00 of this payment is left to refund." }]
    await form.amount.fill("1150.5")
    assert.equal(await form.amount.getAttribute("aria-invalid"), "false")
    await form.submit.click()
    await page.getByText("Only $50.00 of this payment is left to refund.").waitFor()
    assert.equal(server.refundAsks.length, 1)
  })

  test("pressing twice asks the gateway once", async () => {
    const server = new Server()
    server.refundAnswers = [{ status: "pending", message: "Refund asked of the gateway.", delay: 400 }]
    const page = await opened(server)
    const form = await startRefund(page)
    await form.amount.fill("100")
    await form.reason.fill("Guest cancelled")
    // two presses in one go, before the screen can re-render between them
    await form.submit.evaluate((b) => { b.click(); b.click() })
    await page.getByText("Refund asked of the gateway.").waitFor()
    await form.submit.click({ force: true, timeout: 1000 }).catch(() => {})
    await page.waitForTimeout(300)
    assert.equal(server.refundAsks.length, 1)
    const [ask] = server.refundAsks
    assert.equal(ask.folio, FOLIO)
    assert.equal(ask.reference, GATEWAY_REF)
    assert.equal(ask.amount, 100)
    assert.equal(ask.reason, "Guest cancelled")
    assert.match(String(ask.idempotency_key), /\S{8,}/)
  })

  test("a retry of the same refund carries the same key, a new refund a new one", async () => {
    const server = new Server()
    server.refundAnswers = [
      { abort: true },                                  // the answer is lost: api.ts retries once
      { error: "The gateway did not answer." },         // the desk sees why…
      { status: "pending", message: "Refund asked of the gateway." },  // …and presses again
      { status: "pending", message: "Refund asked of the gateway." },
    ]
    const page = await opened(server)
    let form = await startRefund(page)
    await form.amount.fill("100")
    await form.reason.fill("Guest cancelled")
    await form.submit.click()
    await page.getByText("The gateway did not answer.").waitFor()
    await form.submit.click()
    await page.getByText("Refund asked of the gateway.").waitFor()
    assert.equal(server.refundAsks.length, 3)
    const keys = server.refundAsks.map((a) => a.idempotency_key)
    assert.equal(new Set(keys).size, 1, "one refund, one key")

    form = await startRefund(page)
    await form.amount.fill("50")
    await form.reason.fill("Second refund")
    await form.submit.click()
    await page.waitForTimeout(300)
    assert.equal(server.refundAsks.length, 4)
    assert.notEqual(server.refundAsks[3].idempotency_key, keys[0], "another refund, another key")
  })

  test("a refusal keeps what the desk typed while it waited", async () => {
    const server = new Server()
    server.refundAnswers = [{ error: "The gateway did not answer.", delay: 600 }]
    const page = await opened(server)
    const form = await startRefund(page)
    await form.amount.fill("100")
    await form.reason.fill("Guest cancelled")
    await form.submit.click()
    await form.reason.fill("Guest cancelled, asked by phone")
    await page.getByText("The gateway did not answer.").waitFor()
    assert.equal(await form.reason.inputValue(), "Guest cancelled, asked by phone")
  })

  test("the Refund row appears when the gateway reports it", async () => {
    const server = new Server()
    const page = await opened(server)
    const form = await startRefund(page)
    await form.amount.fill("100")
    await form.reason.fill("Guest cancelled")
    await form.submit.click()
    await page.getByText("Refund asked of the gateway.").waitFor()
    // the gateway's payment.refunded lands on the folio a little later
    server.payments.push({
      payment_kind: "Refund", posting_date: "2026-11-19", mode: "Card", amount: -100,
      reference: `refund:${GATEWAY_REF}:r2`,
    })
    server.refundable = { gateway: "Example Pay", payments: [{ reference: GATEWAY_REF, amount: 1250.5, left: 1050.5 }] }
    await page.getByText(`refund:${GATEWAY_REF}:r2`).waitFor({ timeout: 15000 })
    await page.getByText("Refund asked of the gateway.").waitFor({ state: "detached", timeout: 5000 })
    const again = await startRefund(page)
    assert.equal(await again.amount.inputValue(), "1050.5", "what is left now")
  })

  test("in Spanish: the button, the form and what is left", async () => {
    const server = new Server()
    const page = await opened(server, { lang: "es" })
    await row(page, GATEWAY_REF).getByRole("button", { name: "Reembolso", exact: true }).click()
    await page.getByRole("textbox", { name: "Motivo de reembolso" }).fill("Cancela")
    await page.getByRole("spinbutton", { name: "Importe" }).fill("100")
    await page.getByText(/disponibles/).waitFor()
    await page.getByRole("button", { name: /^Reembolsar \$/ }).click()
    await page.getByText("Refund asked of the gateway.").waitFor()
    assert.equal(server.refundAsks.length, 1)
  })
})
