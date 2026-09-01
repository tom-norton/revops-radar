#!/usr/bin/env node
/**
 * Renders one CV spec into a .docx.
 *
 *   node cv/build-cv.js <spec.json> <out.docx>
 *
 * The split this file exists to enforce: a model chooses the WORDS, this chooses the
 * LAYOUT. Every measurement below is fixed and comes from the skill's Step 8a. Nothing
 * in the spec can move a margin, a font size or a tab stop, because the failure mode
 * that matters here is silent -- a CV with the dates in the wrong place looks fine to
 * the code that produced it and wrong to every recruiter who opens it.
 *
 * Spec shape (see cv-base.json for a filled-in example):
 *   { theme: {font, accent}, name, contact: [{text, href?}], role_title, summary,
 *     sections: [{heading, entries: [{left, right, sub_left, sub_right, bullets: []}]}],
 *     skills }
 */

const fs = require("fs");
const {
  Document, Packer, Paragraph, TextRun, ExternalHyperlink,
  AlignmentType, BorderStyle, TabStopType, LevelFormat,
} = require("docx");

// ---------------------------------------------------------------- fixed measurements

// US Letter, 0.75" margins all round. 1 inch = 1440 twips.
const PAGE_W = 12240;
const PAGE_H = 15840;
const MARGIN = 1080;
// Right-aligned tab stop at the content width: 12240 - 2*1080. This is the one number
// that has to be exactly right, because everything dated hangs off it.
const RIGHT_TAB = PAGE_W - 2 * MARGIN; // 10080

// Half-points, because that is what docx wants.
const NAME_PT = 40;      // 20pt
const HEADING_PT = 26;   // 13pt
const BODY_PT = 22;      // 11pt
const CONTACT_PT = 20;   // 10pt

// 240 twips is single spacing, so 1.15 is 276.
const LINE = 276;

const BULLET_INDENT = 288;   // 0.2"
const ENTRY_GAP = 120;       // 6pt between one entry and the next
const CONTACT_SEP = "  ·  ";

// ---------------------------------------------------------------- helpers

function die(msg) {
  console.error(`build-cv: ${msg}`);
  process.exit(1);
}

const spec = (() => {
  const path = process.argv[2];
  const out = process.argv[3];
  if (!path || !out) die("usage: build-cv.js <spec.json> <out.docx>");
  try {
    return JSON.parse(fs.readFileSync(path, "utf8"));
  } catch (e) {
    die(`cannot read spec ${path}: ${e.message}`);
  }
})();

const FONT = (spec.theme && spec.theme.font) || "Calibri";
const ACCENT = ((spec.theme && spec.theme.accent) || "1F6F78").replace(/^#/, "");

const txt = (v) => String(v === undefined || v === null ? "" : v);

/** A paragraph with the house line spacing, unless told otherwise. */
function para(opts) {
  return new Paragraph({ spacing: { line: LINE, lineRule: "auto" }, ...opts });
}

function run(text, opts = {}) {
  return new TextRun({ text: txt(text), font: FONT, size: BODY_PT, ...opts });
}

/** The teal rule under a section heading. Also used by the role-title header, which the
 *  skill says takes the same style. */
const accentRule = {
  bottom: { style: BorderStyle.SINGLE, size: 6, color: ACCENT, space: 2 },
};

// ---------------------------------------------------------------- blocks

function nameHeader() {
  return para({
    alignment: AlignmentType.CENTER,
    spacing: { line: LINE, lineRule: "auto", after: 20 },
    children: [run(spec.name, { size: NAME_PT, bold: true, color: ACCENT })],
  });
}

/** One centered 10pt line. Anything carrying an href becomes a real ExternalHyperlink,
 *  not blue text that does nothing when clicked. */
function contactLine() {
  const items = (spec.contact || []).filter((c) => txt(c && c.text).trim());
  const children = [];
  items.forEach((item, i) => {
    if (i) children.push(run(CONTACT_SEP, { size: CONTACT_PT }));
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
    spacing: { line: LINE, lineRule: "auto", after: 160 },
    children,
  });
}

/** 13pt bold ALL CAPS, left, teal, bottom border. Section headings and the role-title
 *  header are the same object on purpose -- the skill defines the latter as "the same
 *  style as other section headers". */
function heading(text, extra = {}) {
  return para({
    border: accentRule,
    spacing: { line: LINE, lineRule: "auto", before: 200, after: 80, ...extra },
    children: [run(txt(text).toUpperCase(), {
      size: HEADING_PT, bold: true, color: ACCENT,
    })],
  });
}

/** left [right-tab] right. The tab stop is what puts dates and locations on the right
 *  margin; without it they sit wherever the text happens to end. */
function tabbedLine(left, right, leftOpts = {}, rightOpts = {}, after = 0) {
  const children = [run(left, leftOpts)];
  if (txt(right).trim()) children.push(run(`\t${txt(right)}`, rightOpts));
  return para({
    tabStops: [{ type: TabStopType.RIGHT, position: RIGHT_TAB }],
    spacing: { line: LINE, lineRule: "auto", after },
    children,
  });
}

function bullet(text, after) {
  return para({
    numbering: { reference: "cv-bullets", level: 0 },
    indent: { left: BULLET_INDENT, hanging: BULLET_INDENT },
    spacing: { line: LINE, lineRule: "auto", after },
    children: [run(text)],
  });
}

/** One employer, degree or project: the tabbed header lines, then its bullets.
 *
 *  The 6pt gap that separates one entry from the next is passed into whichever paragraph
 *  ends up last, not patched on afterwards -- docx builds its XML at construction, so
 *  assigning to a finished Paragraph does nothing at all and does it silently. It is a
 *  gap rather than an empty paragraph because an empty paragraph is a whole line of
 *  height, and three of those are what push a two-page CV onto a third. */
function entryBlock(entry) {
  const out = [];
  const bullets = (entry.bullets || []).filter((b) => txt(b).trim());
  const hasHead = txt(entry.left).trim() || txt(entry.right).trim();
  const hasSub = txt(entry.sub_left).trim() || txt(entry.sub_right).trim();
  const tailAfter = ENTRY_GAP;

  if (hasHead) {
    out.push(tabbedLine(entry.left, entry.right, { bold: true }, {},
                        (hasSub || bullets.length) ? 0 : tailAfter));
  }
  if (hasSub) {
    out.push(tabbedLine(entry.sub_left, entry.sub_right, { italics: true },
                        { italics: true }, bullets.length ? 0 : tailAfter));
  }
  bullets.forEach((b, i) => out.push(bullet(b, i === bullets.length - 1 ? tailAfter : 0)));
  return out;
}

// ---------------------------------------------------------------- document

const body = [nameHeader(), contactLine()];

if (txt(spec.role_title).trim()) body.push(heading(spec.role_title, { before: 0 }));
if (txt(spec.summary).trim()) {
  body.push(para({
    spacing: { line: LINE, lineRule: "auto", after: 80 },
    children: [run(spec.summary)],
  }));
}

(spec.sections || []).forEach((section) => {
  const entries = (section.entries || []).filter(
    (e) => txt(e.left).trim() || txt(e.sub_left).trim() || (e.bullets || []).length);
  if (!entries.length) return;
  body.push(heading(section.heading));
  entries.forEach((e) => entryBlock(e).forEach((p) => body.push(p)));
});

if (txt(spec.skills).trim()) {
  body.push(heading("Skills"));
  body.push(para({
    alignment: AlignmentType.CENTER,
    children: [run(spec.skills)],
  }));
}

const doc = new Document({
  creator: spec.name || "",
  title: `${spec.name || "CV"} - ${spec.role_title || ""}`.trim(),
  styles: {
    // Calibri on every run in the document, belt and braces: the runs above set it
    // explicitly, and this catches anything docx adds on its own (list markers, for one).
    default: {
      document: { run: { font: FONT, size: BODY_PT } },
      hyperlink: { run: { font: FONT, color: "0563C1", underline: {} } },
    },
  },
  numbering: {
    config: [{
      reference: "cv-bullets",
      levels: [{
        level: 0,
        format: LevelFormat.BULLET,
        text: "•",
        alignment: AlignmentType.LEFT,
        style: {
          run: { font: FONT, size: BODY_PT },
          paragraph: { indent: { left: BULLET_INDENT, hanging: BULLET_INDENT } },
        },
      }],
    }],
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
  console.log(`build-cv: wrote ${process.argv[3]} (${buf.length} bytes, ` +
              `${body.length} paragraphs)`);
}).catch((e) => die(e.stack || e.message));
