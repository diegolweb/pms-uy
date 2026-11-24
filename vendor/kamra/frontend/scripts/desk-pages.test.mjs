// The country pack's own pages in the menu (kamra.localization.desk_pages,
// on kamra.api.property_locale as `desk_pages`): Uruguay's pending invoices
// (/facturas) show at the end of Front Desk, before its Settings, open in a
// new tab, and the command palette finds them - it reads the same list
// (lib/apps.ts visibleApps). The real AppShell runs in headless Chromium
// against a stubbed server:
//
//   node --test scripts/desk-pages.test.mjs
//
// Needs this frontend's node_modules (esbuild comes with vite, playwright is
// a dev dependency) and playwright's Chromium. No network.

import assert from "node:assert/strict"
import path from "node:path"
import { after, before, beforeEach, describe, test } from "node:test"
import { build } from "esbuild"
import { viteGlob } from "./esbuild-vite-glob.mjs"
import { chromium } from "playwright"

const SRC = path.resolve(import.meta.dirname, "../src")
const ORIGIN = "http://kamra.test"

const ENTRY = `
import { createRoot } from "react-dom/client"
import { MemoryRouter, Route, Routes } from "react-router-dom"
import { AuthProvider } from "./lib/auth"
import AppShell from "./AppShell"
import { initLang } from "./lib/dir"
import { APPS, visibleApps, withDeskPages } from "./lib/apps"

;(window as any).__apps = { APPS, visibleApps, withDeskPages }
initLang().then(() => createRoot(document.getElementById("root")!).render(
  <MemoryRouter initialEntries={["/"]}>
    <AuthProvider>
      <Routes>
        <Route element={<AppShell />}>
          <Route path="/" element={<p>Today</p>} />
        </Route>
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

const PAGES = [{ label: "Facturas pendientes", url: "/facturas?property=Hotel%20T",
                 roles: ["Front Desk", "Finance", "Hotel Admin"] }]

const LOCALE = {
  currency_symbol: "$", locale: "es-UY", currency: "UYU", tax_label: "IVA", tax_id_label: "RUT",
  tax_rates: [0, 10, 22], id_types: [], id_type_hints: {}, payment_modes: ["Cash"],
  instant_payment_modes: [], outlet_payment_modes: ["Cash"], default_nationality: "", adult_age: 18,
  minor_ids: "foreign", fnb_charge_words: [],
  integrations: { payment_gateways: [], tax_return: false, verify_tax_id: false, verify_guest_id: false },
}

class Server {
  constructor({ pages = [], roles = ["Front Desk"] } = {}) {
    this.pages = pages
    this.roles = roles
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
        return reply({ user: "desk@example.com", full_name: "Desk", roles: this.roles })
      case "kamra.api.property_locale":
        return reply({ ...LOCALE, desk_pages: this.pages })
      case "kamra.public_api.site_info":
        return reply({ demo_mode: false, source_code: [] })
      case "kamra.api.my_properties":
        return reply([{ name: "Hotel T", property_name: "Hotel T", city: null }])
      case "kamra.api.enabled_modules":
        return reply([])
      case "kamra.apps.my_apps":
        return reply([])
      case "kamra.notifications.bell":
        return reply({ modules: [], unread: 0, items: [] })
      default:
        // everything else the shell asks (properties, modules, notifications…): nothing
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

async function opened(server, { lang = "en" } = {}) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } })
  await context.addInitScript((l) => {
    localStorage.setItem("kamra-lang", l)
    localStorage.setItem("kamra_property", "Hotel T")
    sessionStorage.setItem("kamra:app", "front-desk")
  }, lang)
  const page = await context.newPage()
  pages.push(page)
  page.setDefaultTimeout(5000)
  page.errors = []
  page.on("pageerror", (e) => page.errors.push(String(e)))
  await page.route(`${ORIGIN}/**`, (route) => server.answer(route))
  await page.goto(`${ORIGIN}/`)
  if (process.env.DEBUG_DESK) {
    await page.waitForTimeout(2000)
    console.log("errors:", page.errors, "unknown:", server.unknown, "\nbody:", (await page.innerText("body")).slice(0, 1200))
  }
  await page.waitForFunction(() => !!window.__apps)
  return page
}

describe("The pack's pages in the menu", () => {
  test("none: the apps are Kamra's own, untouched", async () => {
    const page = await opened(new Server())
    assert.equal(await page.evaluate(() => {
      const { APPS, withDeskPages } = window.__apps
      return withDeskPages(APPS, []) === APPS && withDeskPages(APPS, undefined) === APPS
    }), true)
  })

  test("a page goes at the end of Front Desk, before its Settings, as a link", async () => {
    const page = await opened(new Server())
    const out = await page.evaluate((list) => {
      const { APPS, withDeskPages } = window.__apps
      const apps = withDeskPages(APPS, list)
      const items = apps.find((a) => a.id === "front-desk").items
      const at = items.findIndex((i) => i.href === list[0].url)
      return {
        at, label: items[at]?.label, to: items[at]?.to, roles: items[at]?.roles, icon: !!items[at]?.icon,
        next: items[at + 1]?.to, grew: items.length - APPS.find((a) => a.id === "front-desk").items.length,
        others: apps.filter((a) => a.id !== "front-desk").every((a) => APPS.includes(a)),
        bare: apps.find((a) => a.id === "front-desk") !== APPS.find((a) => a.id === "front-desk"),
        noRoles: withDeskPages(APPS, [{ label: "X", url: "/x" }]).find((a) => a.id === "front-desk")
          .items.find((i) => i.href === "/x").roles,
      }
    }, PAGES)
    assert.ok(out.at > 0, "the page is in Front Desk")
    assert.equal(out.label, "Facturas pendientes")
    assert.equal(out.to, undefined, "a link out of the app, not a route")
    assert.deepEqual(out.roles, ["Front Desk", "Finance", "Hotel Admin"])
    assert.equal(out.icon, true)
    assert.equal(out.next, "/settings/front-desk")
    assert.equal(out.grew, 1)
    assert.equal(out.others, true, "the other apps are untouched")
    assert.equal(out.bare, true, "the registry itself is not changed")
    assert.equal(out.noRoles, undefined, "a page without roles is for everyone who sees Front Desk")
  })

  test("the sidebar shows the property's pages as a link to a new tab", async () => {
    const page = await opened(new Server({ pages: PAGES }))
    const link = page.getByRole("link", { name: "Facturas pendientes" }).first()
    await link.waitFor()
    assert.equal(await link.getAttribute("href"), "/facturas?property=Hotel%20T")
    assert.equal(await link.getAttribute("target"), "_blank")
    // visibleApps reads them from the locale by default: the palette's list too
    assert.equal(await page.evaluate(() => window.__apps.visibleApps(["Front Desk"])
      .find((a) => a.id === "front-desk").items.some((i) => i.href?.startsWith("/facturas"))), true)
  })

  test("a property whose pack has none shows no such link", async () => {
    const page = await opened(new Server())
    await page.getByRole("link", { name: "Billing" }).first().waitFor()
    assert.equal(await page.getByRole("link", { name: "Facturas pendientes" }).count(), 0)
  })

  test("the page's own roles still gate it", async () => {
    const page = await opened(new Server({
      pages: [{ ...PAGES[0], roles: ["Finance"] }], roles: ["Front Desk"],
    }))
    await page.getByRole("link", { name: "Billing" }).first().waitFor()
    await page.waitForTimeout(300)
    assert.equal(await page.getByRole("link", { name: "Facturas pendientes" }).count(), 0)
  })
})
