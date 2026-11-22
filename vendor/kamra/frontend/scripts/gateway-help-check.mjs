#!/usr/bin/env node
/** Regression checks for the Payments settings card (HR-123): the field hints and the setup help are the chosen
 * gateway's own words, from its provider's info() (kamra.payments.gateway_info). A gateway that brings its own words
 * never shows the Razorpay or Cashfree ones; Razorpay and Cashfree keep theirs.
 * Renders the real card (screens/settings/cards.tsx) with the real translator and the real dictionaries:
 * `node scripts/gateway-help-check.mjs`. */
import assert from "node:assert/strict"
import fs from "node:fs"
import path from "node:path"
import { createRequire } from "node:module"
import { fileURLToPath } from "node:url"
import ts from "typescript"

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..")
const require = createRequire(import.meta.url)
const React = require("react")
const { renderToStaticMarkup } = require("react-dom/server")

const dictionaries = { en: {} }
for (const language of ["es", "pt", "ar"]) {
  dictionaries[language] = JSON.parse(fs.readFileSync(path.join(root, `src/i18n/locales/${language}.json`), "utf8"))
}
let language = "en"

function load(file, imports = {}) {
  const output = ts.transpileModule(fs.readFileSync(path.join(root, file), "utf8"), {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
      jsx: ts.JsxEmit.ReactJSX,
      esModuleInterop: true,
    },
  }).outputText
  const api = {}
  new Function("require", "exports", output)((name) => {
    if (name in imports) return imports[name]
    throw new Error(`Unexpected import in ${file}: ${name}`)
  }, api)
  return api
}

const i18n = load("src/lib/i18n.ts", {
  react: React,
  "./dir": { getLang: () => language },
  "./locales": { dictionaryFor: (key) => dictionaries[key] ?? {} },
})
// Server rendering reads the language from useSyncExternalStore's server snapshot (always "en"); the card is
// rendered here through the real pure translator instead, so Spanish shows what a Spanish screen shows.
const useT = () => ({ lang: language, t: i18n.t, tn: i18n.tn })

const passthrough = (tag) => ({ children, className }) => React.createElement(tag, { className }, children)
const unused = (name) => () => {
  throw new Error(`${name} is not expected while rendering the Payments card`)
}
const cards = load("src/screens/settings/cards.tsx", {
  react: React,
  "react/jsx-runtime": require("react/jsx-runtime"),
  "react-router-dom": { Link: passthrough("a") },
  "../../lib/api": { call: unused("call") },
  "../../lib/resource": {
    createResource: unused("createResource"),
    listResource: unused("listResource"),
    serverError: String,
    updateResource: unused("updateResource"),
  },
  "../../lib/theme": { getTheme: () => "light", setTheme: () => {} },
  "../../lib/dir": { getLang: () => language, setLang: () => {}, LANGS: [] },
  "../../lib/i18n": { ...i18n, useT, useUILanguage: () => language },
  "../../lib/realtime": { probeSocket: unused("probeSocket") },
  "../../components/ui/button": {
    Button: ({ children, disabled }) => React.createElement("button", { disabled }, children),
  },
  "../../components/ImageField": { __esModule: true, default: () => null },
  "../../components/ui/card": {
    Card: passthrough("section"),
    CardHeader: passthrough("header"),
    CardContent: passthrough("div"),
    CardTitle: passthrough("h3"),
  },
  "../../lib/money": { cur: String, moneyLocale: () => "en", useLocale: unused("useLocale") },
})

assert.equal(typeof cards.PaymentGatewayCard, "function", "cards.tsx exports the Payments card (PaymentGatewayCard)")
assert.equal(typeof cards.chosenGateway, "function", "cards.tsx exports chosenGateway")

const decode = (html) =>
  html
    .replace(/<[^>]+>/g, " ")
    .replace(/&#x27;/g, "'")
    .replace(/&quot;/g, '"')
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&amp;/g, "&")
    .replace(/\s+/g, " ")
    .trim()

/** The rendered card: its whole text, each field (one <label> each) and each paragraph of the setup help (the box
 * after the settings card, which renders as the stub <section>). */
function render({ gateways, chosen, info, doc = {} }) {
  const html = renderToStaticMarkup(
    React.createElement(cards.PaymentGatewayCard, {
      gateways,
      chosen,
      doc: { gateway: chosen, ...doc },
      info,
      onSave: async () => {},
    }),
  )
  const [settings, help = ""] = html.split("</section>")
  const fields = [...settings.matchAll(/<label[^>]*>([\s\S]*?)<\/label>/g)].map((m) => decode(m[1]))
  const paragraphs = [...help.matchAll(/<p[^>]*>([\s\S]*?)<\/p>/g)].map((m) => ({ html: m[1], text: decode(m[1]) }))
  return { html, text: decode(html), fields, paragraphs }
}
const field = (card, label) => {
  const found = card.fields.filter((f) => f.startsWith(label))
  assert.equal(found.length, 1, `one field labelled ${JSON.stringify(label)} in ${JSON.stringify(card.fields)}`)
  return found[0]
}
const noIndianGateway = (card, why) => assert.doesNotMatch(card.html, /razorpay|cashfree/i, why)

const host = "https://hotel.example.com"
const MP_TODAY = {
  gateway: "Mercado Pago",
  webhook_url: `${host}/api/method/kamra_uy.payments.webhook.core_event`,
  signup_url: "https://www.mercadopago.com.uy/",
  needs_webhook: false,
}
// What the provider says about its own fields and setup (test words: the real ones are the provider's).
const MP_WORDS = {
  fields: {
    key_id: { label: "Código de la propiedad", hint: "el código de esta propiedad en el servicio de cobros" },
    key_secret: { label: "Clave del servicio", hint: "la clave del servicio de cobros" },
    webhook_secret: { label: "Secreto de firma", hint: "con él firma el servicio de cobros sus avisos" },
  },
  help: [
    "El servicio de cobros avisa de cada pago a {webhook_url}, firmado con el secreto de firma.",
    "En modo prueba no se pide ningún enlace.",
  ],
}
const MP = { ...MP_TODAY, ...MP_WORDS }
const RAZORPAY = {
  gateway: "Razorpay",
  webhook_url: `${host}/api/method/kamra.payments.razorpay_webhook`,
  signup_url: "https://razorpay.com/",
  needs_webhook: true,
}
const CASHFREE = {
  gateway: "Cashfree",
  webhook_url: `${host}/api/method/kamra.payments.cashfree_webhook`,
  signup_url: "https://www.cashfree.com/",
  needs_webhook: false,
}
const INDIA = ["Razorpay", "Cashfree"]
const URUGUAY = ["Mercado Pago"]
const INDIA_KEY_HINT = "Razorpay: Account & Settings → API Keys. Cashfree: Developers → API Keys (App ID)"
const INDIA_SECRET_HINT =
  "Razorpay only: set the same secret on the Razorpay webhook. Cashfree signs with the secret key"
const RAZORPAY_HELP = "In Razorpay, add a webhook for the payment_link.paid event pointing to"
const CASHFREE_HELP = "Nothing to set up in Cashfree: every link reports back to"
const CASHFREE_MOBILE = "Guests need a mobile number on their profile to receive a Cashfree link."
const SIGNUP = "Create one free"

// ---- Mercado Pago, with its words in info(): the hints and the help are exactly the provider's.
for (const lang of ["en", "es", "pt"]) {
  language = lang
  const card = render({ gateways: URUGUAY, chosen: "Mercado Pago", info: MP })
  noIndianGateway(card, `${lang}: Mercado Pago shows no Razorpay or Cashfree text`)
  for (const [name, words] of Object.entries(MP_WORDS.fields)) {
    assert.equal(field(card, words.label), `${words.label} ${words.hint}`, `${lang}: ${name} in Mercado Pago's words`)
  }
  for (const india of ["Key ID / App ID", "Key secret / Secret key", "Webhook secret"]) {
    assert.ok(!card.text.includes(i18n.t(india)), `${lang}: no ${india} label when the provider names the field`)
  }
  const help = card.paragraphs.map((p) => p.text)
  assert.ok(help.includes(`El servicio de cobros avisa de cada pago a ${MP.webhook_url} , firmado con el secreto de firma.`),
    `${lang}: the first help paragraph, with the webhook URL where the provider put it: ${JSON.stringify(help)}`)
  assert.ok(help.includes("En modo prueba no se pide ningún enlace."), `${lang}: the second help paragraph`)
  const withUrl = card.paragraphs.find((p) => p.text.startsWith("El servicio de cobros"))
  assert.match(withUrl.html, new RegExp(`<code[^>]*>${MP.webhook_url.replace(/[.]/g, "\\.")}</code>`),
    `${lang}: the webhook URL is the copyable code element`)
  assert.ok(!withUrl.html.includes("{webhook_url}"), `${lang}: no placeholder left in the help`)
  assert.ok(card.text.includes(i18n.t("No {gateway} account yet?", { gateway: "Mercado Pago" })),
    `${lang}: without keys, the sign-up line names Mercado Pago`)
}
language = "en"

// The sign-up line goes once the property has its keys; the provider's help stays.
{
  const card = render({ gateways: URUGUAY, chosen: "Mercado Pago", info: MP, doc: { key_id: "hr-propiedad-1" } })
  assert.ok(!card.text.includes(SIGNUP), "no sign-up line with keys")
  assert.ok(card.text.includes("En modo prueba no se pide ningún enlace."), "the help stays with keys")
}

// A provider that names only some fields: the others keep Kamra's generic label and get no other gateway's hint.
{
  const info = { ...MP_TODAY, fields: { key_id: MP_WORDS.fields.key_id } }
  const card = render({ gateways: URUGUAY, chosen: "Mercado Pago", info })
  noIndianGateway(card, "a partial provider never falls back to the Razorpay or Cashfree hints")
  assert.equal(field(card, "Código de la propiedad"), "Código de la propiedad el código de esta propiedad en el servicio de cobros")
  assert.equal(field(card, "Webhook secret"), "Webhook secret", "webhook_secret: generic label, no hint")
  assert.equal(field(card, "Key secret / Secret key"), "Key secret / Secret key", "key_secret: generic label, no hint")
  assert.ok(!card.paragraphs.some((p) => p.text.includes(MP.webhook_url)), "no help paragraph the provider did not give")
}

// Nothing to say (keys set, no help of its own): no empty help box.
for (const info of [MP_TODAY, { ...MP_TODAY, fields: MP_WORDS.fields }]) {
  const card = render({ gateways: URUGUAY, chosen: "Mercado Pago", info, doc: { key_id: "hr-propiedad-1" } })
  assert.ok(!card.html.includes("bg-zinc-50"), `no empty help box: ${card.html.split("</section>")[1]}`)
}

// Mercado Pago's info() as it is today (no words of its own), and before info() answers: still no India text.
for (const info of [MP_TODAY, null]) {
  for (const lang of ["en", "es"]) {
    language = lang
    const card = render({ gateways: URUGUAY, chosen: "Mercado Pago", info })
    noIndianGateway(card, `${lang}, info ${info ? "without words" : "not loaded"}: no Razorpay or Cashfree text`)
    assert.equal(field(card, i18n.t("Key ID / App ID")), i18n.t("Key ID / App ID"), `${lang}: key_id without a hint`)
    assert.equal(field(card, i18n.t("Webhook secret")), i18n.t("Webhook secret"), `${lang}: webhook_secret without a hint`)
  }
}
language = "en"

// ---- Razorpay and Cashfree: the same texts as before HR-123, in every language.
for (const lang of ["en", "es", "pt", "ar"]) {
  language = lang
  for (const [chosen, info] of [["Razorpay", RAZORPAY], ["Cashfree", CASHFREE]]) {
    for (const loaded of [info, null]) {
      const card = render({ gateways: INDIA, chosen, info: loaded })
      const when = `${lang}, ${chosen}${loaded ? "" : " before info() answers"}`
      assert.equal(field(card, i18n.t("Key ID / App ID")), `${i18n.t("Key ID / App ID")} ${i18n.t(INDIA_KEY_HINT)}`,
        `${when}: key_id hint as before`)
      assert.equal(field(card, i18n.t("Key secret / Secret key")), i18n.t("Key secret / Secret key"),
        `${when}: key_secret as before`)
      assert.equal(field(card, i18n.t("Webhook secret")), `${i18n.t("Webhook secret")} ${i18n.t(INDIA_SECRET_HINT)}`,
        `${when}: webhook_secret hint as before`)
      if (!loaded) assert.equal(card.paragraphs.length, 0, `${when}: no help box yet, as before`)
    }
  }
  const razorpay = render({ gateways: INDIA, chosen: "Razorpay", info: RAZORPAY }).paragraphs.map((p) => p.text)
  assert.ok(razorpay.includes(`${i18n.t(RAZORPAY_HELP)} ${RAZORPAY.webhook_url}`), `${lang}: Razorpay webhook help as before`)
  assert.ok(!razorpay.some((p) => p.includes(i18n.t(CASHFREE_HELP))), `${lang}: Razorpay shows no Cashfree help`)
  const cashfree = render({ gateways: INDIA, chosen: "Cashfree", info: CASHFREE }).paragraphs.map((p) => p.text)
  assert.ok(cashfree.includes(`${i18n.t(CASHFREE_HELP)} ${CASHFREE.webhook_url} . ${i18n.t(CASHFREE_MOBILE)}`),
    `${lang}: Cashfree help as before: ${JSON.stringify(cashfree)}`)
  assert.ok(!cashfree.some((p) => p.includes(i18n.t(RAZORPAY_HELP))), `${lang}: Cashfree shows no Razorpay help`)
}
language = "en"
{
  const card = render({ gateways: INDIA, chosen: "Cashfree", info: CASHFREE })
  const options = [...card.html.matchAll(/<option value="([^"]*)"( selected="")?>/g)].map((m) => [m[1], !!m[2]])
  assert.deepEqual(options, [["Razorpay", false], ["Cashfree", true]],
    "the gateway select still offers the pack's gateways in order, the chosen one selected")
  assert.ok(card.text.includes(`No Cashfree account yet? ${SIGNUP}`), "sign-up line as before")
}

// ---- The help follows the gateway chosen in the form, then the saved one, then the pack's first.
assert.equal(cards.chosenGateway(INDIA, "Razorpay", "Cashfree"), "Cashfree", "the one picked in the form")
assert.equal(cards.chosenGateway(INDIA, "Cashfree", undefined), "Cashfree", "the saved one")
assert.equal(cards.chosenGateway(INDIA, "Cashfree", "Stripe"), "Cashfree", "a pick the pack does not offer is ignored")
assert.equal(cards.chosenGateway(URUGUAY, "Razorpay", undefined), "Mercado Pago", "a saved gateway of another pack")
assert.equal(cards.chosenGateway(URUGUAY, undefined, undefined), "Mercado Pago", "nothing saved yet")
assert.equal(cards.chosenGateway(URUGUAY, "", ""), "Mercado Pago", "blank values")
assert.equal(cards.chosenGateway([], "Razorpay", "Cashfree"), "", "no gateway in the pack")

console.log("gateway help checks OK")
