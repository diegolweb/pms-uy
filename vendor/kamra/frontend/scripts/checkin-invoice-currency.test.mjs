// The currency a stay is invoiced in, picked at check-in: where the country
// pack offers it (kamra.localization.invoice_currency, Uruguay: dollars or
// pesos), kamra.checkin.checkin_flow carries `invoice_currency` and the last
// screen of the guided check-in shows the choice with the pack's proposal
// picked and the note of the one picked (the rate the invoice goes out with).
// The desk may pick the other one; kamra.api.check_in takes the pick. Without
// an offer there is nothing to pick and check_in goes as before. The real
// CheckInDialog runs in headless Chromium against a stubbed server:
//
//   node --test scripts/checkin-invoice-currency.test.mjs
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
const RES = "RES-T-0001"

const ENTRY = `
import { createRoot } from "react-dom/client"
import { MemoryRouter } from "react-router-dom"
import { AuthProvider } from "./lib/auth"
import CheckInDialog from "./components/CheckInDialog"
import { initLang } from "./lib/dir"

initLang().then(() => createRoot(document.getElementById("root")!).render(
  <MemoryRouter>
    <AuthProvider>
      <CheckInDialog reservation="${RES}" onClose={() => {}} onDone={() => {}} />
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
    loader: { ".png": "dataurl", ".svg": "dataurl", ".jpg": "dataurl", ".woff2": "dataurl", ".mp3": "dataurl" },
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

// --- what the Uruguay pack offers (kamra_uy.fiscal.model.currency_choice) ------

const options = [
  { code: "USD", label: "Dólares (USD)", note: "El CFE lleva la cotización del BCU del 19-12-2026: 1 USD = 40,123 UYU." },
  { code: "UYU", label: "Pesos uruguayos (UYU)",
    note: "Los importes se convierten de USD con la cotización del BCU del 19-12-2026: 1 USD = 40,123 UYU." },
]
const HIGH_SEASON = {
  title: "Moneda de la factura",
  text: "Se propone en dólares: el check-in cae en la alta temporada (del 16 de noviembre al domingo de Turismo).",
  options,
  currency: "USD",
}
const LOW_SEASON = {
  ...HIGH_SEASON,
  text: "Se propone en pesos: el check-in cae fuera de la alta temporada.",
  currency: "UYU",
}

// --- the stubbed server -------------------------------------------------------

function flow(offer) {
  return {
    reservation: {
      name: RES, status: "Confirmed", guest: "G-1", guest_name: "Test Guest", room_type: "RT-1",
      check_in_date: "2026-12-20", check_out_date: "2026-12-22", nights: 2, adults: 2, children: 0, vip: 0,
      source: "Direct", special_requests: "", checked_in_by: null, checked_in_at: null,
    },
    guest: {
      name: "G-1", full_name: "Test Guest", phone: null, email: "guest@example.com", nationality: null,
      id_type: null, id_number: null, id_on_file: true, id_expiry_date: null, id_status: "none", id_used_count: 0,
      has_photo: false, gender: null, date_of_birth: null, address: "",
    },
    vocabulary: { id_types: [] },
    steps: [],
    residence: null,
    invoice_currency: offer,
    missing_required: [],
    pending_backfill: [],
    rooms: [],
    request_presets: [],
    till: null,
    ids: { ok: true, missing: [], online: false, precheckin_status: "", precheckin_on: null, occupants: [] },
    billing: {
      payer: "Guest", source: "Direct", channel: null, ota_ref: null, pay_at_hotel: 0, company: null,
      company_name: null, billing_rules: [], group_booking: null, group_name: null, stay_total: 200,
      advance_paid: 0, balance_due: 200, options: ["Guest"],
    },
    room: {
      room: "R-101", room_number: "101", housekeeping_status: "Clean", entire_place: null,
      room_type_name: "Doble", assigned_by: null, assigned_at: null,
    },
    deposit: { applies: false, satisfied: true, security: null, advance: null, security_due: 0, advance_due: 0 },
    grc: { signed: false },
    keys: { count: 1, cards: "", issued_by: null, adapter: "", adapter_label: "", manual: true },
    stay_info: {
      guest_wifi_name: "", guest_wifi_password: "", breakfast_hours: "", emergency_number: "",
      guest_info_note: "", checkout_time: "", configured: false,
    },
    room_charges: { allowed: true, recommend_block: false },
    records: {},
  }
}

class Server {
  constructor(offer) {
    this.flow = flow(offer)
    this.checkIns = []
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
      case "kamra.checkin.checkin_flow":
        return reply(this.flow)
      case "kamra.api.check_in":
        this.checkIns.push(route.request().postDataJSON())
        return reply({ ok: true, reservation: RES, room: "R-101", checked_in_by: "desk@example.com" })
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

async function opened(offer, { lang = "en" } = {}) {
  const server = new Server(offer)
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
  if (process.env.DEBUG_CHECKIN) {
    await page.waitForTimeout(2000)
    console.log("errors:", page.errors, "unknown:", server.unknown,
      "\nbody:", (await page.innerText("body")).slice(0, 1200))
  }
  await page.getByText(/Check-in summary|Resumen del check-in/).waitFor()
  page.server = server
  return page
}

const choice = (page) => page.getByTestId("checkin-invoice-currency")
const picked = async (page) =>
  (await choice(page).getByRole("radio", { checked: true }).allInnerTexts()).map((s) => s.trim())
const note = (page) => page.getByTestId("checkin-invoice-currency-note")

async function checkIn(page) {
  await page.getByRole("button", { name: "Check in to room 101" }).click()
  await page.waitForFunction(() => document.body.innerText.includes("Print registration card"))
  return page.server.checkIns
}

// --- the tests ------------------------------------------------------------------

describe("The invoice currency at check-in", () => {
  test("high season: dollars proposed, with the rate the CFE carries, and check_in takes them", async () => {
    const page = await opened(HIGH_SEASON)
    await choice(page).waitFor()
    assert.match(await choice(page).innerText(), /Moneda de la factura/)
    assert.match(await choice(page).innerText(), /alta temporada/)
    assert.deepEqual(await picked(page), ["Dólares (USD)"])
    assert.equal(await note(page).innerText(), options[0].note)
    const [sent] = await checkIn(page)
    assert.equal(sent.reservation, RES)
    assert.equal(sent.invoice_currency, "USD")
    assert.deepEqual(page.errors, [])
  })

  test("low season: pesos proposed, with the rate they are converted at", async () => {
    const page = await opened(LOW_SEASON)
    await choice(page).waitFor()
    assert.deepEqual(await picked(page), ["Pesos uruguayos (UYU)"])
    assert.equal(await note(page).innerText(), options[1].note)
    const [sent] = await checkIn(page)
    assert.equal(sent.invoice_currency, "UYU")
  })

  test("the desk changes it before checking in, and check_in takes the pick", async () => {
    const page = await opened(LOW_SEASON)
    await choice(page).getByRole("radio", { name: "Dólares (USD)" }).click()
    assert.deepEqual(await picked(page), ["Dólares (USD)"])
    assert.equal(await note(page).innerText(), options[0].note)
    const [sent] = await checkIn(page)
    assert.equal(sent.invoice_currency, "USD")
  })

  test("without an offer there is nothing to pick and check_in goes as before", async () => {
    const page = await opened(null)
    assert.equal(await choice(page).count(), 0)
    const [sent] = await checkIn(page)
    assert.equal(sent.reservation, RES)
    assert.equal("invoice_currency" in sent, false)
  })

  test("a pack that gives no title gets the desk's own, in the desk's language", async () => {
    const page = await opened({ ...HIGH_SEASON, title: "" }, { lang: "es" })
    await choice(page).waitFor()
    assert.match(await choice(page).innerText(), /^Moneda de la factura/)
    assert.equal(await choice(page).getByRole("radiogroup").getAttribute("aria-label"), "Moneda de la factura")
  })
})
