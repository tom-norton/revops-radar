#!/usr/bin/env node
/**
 * Renders one cover-letter spec into a .docx.
 *
 *   node cv/build-letter.js <spec.json> <out.docx>
 *
 * Same split as build-cv.js, for the same reason: a model chooses the WORDS, this chooses
 * the LAYOUT. The letterhead is deliberately identical to the CV's -- same name header,
 * same contact line, same teal, same font and line spacing -- because the two documents
 * arrive together and a letter in a different typeface reads as a letter written by
 * somebody else.
 *
 * Everything below the letterhead is standard business letter format: date, recipient,
 * subject line, salutation, body paragraphs, sign-off. Block style, no first-line indents,
 * a blank line between paragraphs.
 *
 * Spec shape:
 *   { theme: {font, accent}, name, contact: [{text, href?}], contact_separator,
 *     date, recipient: ["Acme", "Amsterdam, Netherlands"], subject,
 *     salutation, paragraphs: ["...", "..."], closing, signature }
 *
 * There is no bullet list in here on purpose. Step 5c bans listicle formatting in a cover
 * letter, and a renderer that cannot draw a bullet is a rule that cannot be forgotten.
 */

const fs = require("fs");
const {
  Document, Packer, Paragraph, TextRun, ExternalHyperlink,
  AlignmentType,
} = require("docx");

// ---------------------------------------------------------------- fixed measurements

// US Letter, 0.75" margins. Identical to the CV: the two print as a set.
const PAGE_W = 12240;
const PAGE_H = 15840;
const MARGIN = 1080;

const NAME_PT = 40;      // 20pt
const BODY_PT = 22;      // 11pt
const CONTACT_PT = 20;   // 10pt

const LINE = 276;        // 1.15

const AFTER_NAME = 40;
// More air than the CV's 160: this is where the letterhead stops and the letter starts.
const AFTER_CONTACT = 240;
const AFTER_DATE = 240;
const AFTER_RECIPIENT_LINE = 0;
const BEFORE_SUBJECT = 240;
const AFTER_SUBJECT = 240;
const AFTER_SALUTATION = 200;
const AFTER_PARAGRAPH = 200;
const BEFORE_CLOSING = 240;
const AFTER_CLOSING = 200;   // the gap a signature would sit in

function die(msg) {
  console.error(`build-letter: ${msg}`);
  process.exit(1);
}

const spec = (() => {
  const path = process.argv[2];
  const out = process.argv[3];
  if (!path || !out) die("usage: build-letter.js <spec.json> <out.docx>");
  try {
    return JSON.parse(fs.readFileSync(path, "utf8"));
  } catch (e) {
    die(`cannot read spec ${path}: ${e.message}`);
  }
})();

const FONT = (spec.theme && spec.theme.font) || "Calibri";
const ACCENT = ((spec.theme && spec.theme.accent) || "117A82").replace(/^#/, "");
const SEP = spec.contact_separator || "  |  ";

const txt = (v) => String(v === undefined || v === null ? "" : v);
const has = (v) => txt(v).trim().length > 0;

function para(opts) {
  return new Paragraph({ spacing: { line: LINE, lineRule: "auto" }, ...opts });
}

function run(text, opts = {}) {
  return new TextRun({ text: txt(text), font: FONT, size: BODY_PT, ...opts });
}

/** A left-aligned line of body text. */
function line(text, after = 0, opts = {}) {
  return para({
    alignment: AlignmentType.LEFT,
    spacing: { line: LINE, lineRule: "auto", after },
    children: [run(text, opts)],
  });
}

// ---------------------------------------------------------------- letterhead

function nameHeader() {
  return para({
    alignment: AlignmentType.CENTER,
    spacing: { line: LINE, lineRule: "auto", after: AFTER_NAME },
    children: [run(spec.name, { size: NAME_PT, bold: true, color: ACCENT })],
  });
}

function contactLine() {
  const items = (spec.contact || []).filter((c) => has(c && c.text));
  const children = [];
  items.forEach((item, i) => {
    if (i) children.push(run(SEP, { size: CONTACT_PT }));
    if (item.href) {
      children.push(new ExternalHyperlink({
        link: item.href,
        children: [run(item.text, { size: CONTACT_PT, style: "Hyperlink" })],
      }));
    } else {
      children.push(run(item.text, { size: CONTACT_PT }));
    }
  });
  return para({
    alignment: AlignmentType.CENTER,
    spacing: { line: LINE, lineRule: "auto", after: AFTER_CONTACT },
    children,
  });
}

// ---------------------------------------------------------------- document

const body = [nameHeader(), contactLine()];

if (has(spec.date)) body.push(line(spec.date, AFTER_DATE));

const recipient = (spec.recipient || []).filter(has);
recipient.forEach((r, i) => {
  body.push(line(r, i === recipient.length - 1 ? 0 : AFTER_RECIPIENT_LINE));
});

if (has(spec.subject)) {
  body.push(para({
    alignment: AlignmentType.LEFT,
    spacing: { line: LINE, lineRule: "auto", before: BEFORE_SUBJECT, after: AFTER_SUBJECT },
    children: [run(spec.subject, { bold: true })],
  }));
}

if (has(spec.salutation)) body.push(line(spec.salutation, AFTER_SALUTATION));

(spec.paragraphs || []).filter(has).forEach((p) => {
  body.push(line(p, AFTER_PARAGRAPH));
});

if (has(spec.closing)) body.push(line(spec.closing, AFTER_CLOSING, {}));
if (has(spec.signature)) body.push(line(spec.signature, 0));

const doc = new Document({
  creator: spec.name || "",
  title: `${spec.name || "Cover letter"} - ${spec.subject || ""}`.trim(),
  styles: {
    default: {
      document: { run: { font: FONT, size: BODY_PT } },
      hyperlink: { run: { font: FONT, color: "0563C1", underline: {} } },
    },
  },
  sections: [{
    properties: {
      page: {
        size: { width: PAGE_W, height: PAGE_H },
        margin: { top: MARGIN, right: MARGIN, bottom: MARGIN, left: MARGIN },
      },
    },
    children: body,
  }],
});

Packer.toBuffer(doc).then((buf) => {
  fs.writeFileSync(process.argv[3], buf);
  console.log(`build-letter: wrote ${process.argv[3]} (${buf.length} bytes, ` +
              `${body.length} paragraphs)`);
}).catch((e) => die(e.stack || e.message));
