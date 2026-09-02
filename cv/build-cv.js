#!/usr/bin/env node
/**
 * Renders one CV spec into a .docx.
 *
 *   node cv/build-cv.js <spec.json> <out.docx>
 *
 * The split this file exists to enforce: a model chooses the WORDS, this chooses the
 * LAYOUT. Every measurement below is read off Tom's real base CVs (Tom_Norton_CV.docx and
 * its Builder and CS siblings), not invented, and nothing in the spec can move a margin, a
 * font size or a tab stop. The failure mode that matters here is silent: a CV with the
 * dates in the wrong place looks fine to the code that produced it and wrong to every
 * recruiter who opens it.
 *
 * Spec shape (cv-base.json is the filled-in example):
 *   { theme: {font, accent}, name, contact: [{text, href?}], contact_separator,
 *     role_title, summary,
 *     sections: [
 *       { heading, kind: "entries",
 *         entries: [{ left, right, roles: [{sub_left, sub_right, bullets}] }] },
 *       { heading, kind: "bullets", bullets: [...] } ],
 *     skills }
 *
 * A bullet is a string, or {lead, text} when it has a bold lead-in. The projects section
 * is the second kind: on the real CV a project is one bullet reading
 * "Factorial (ESADE MBA case study): Built the GTM funnel model..." with the lead-in bold,
 * not an entry with its own header line.
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
// that has to be exactly right, because everything dated hangs off it. The base CVs use
// this exact value.
const RIGHT_TAB = PAGE_W - 2 * MARGIN; // 10080

// Half-points, because that is what docx wants.
const NAME_PT = 40;      // 20pt
const HEADING_PT = 26;   // 13pt
const BODY_PT = 22;      // 11pt
const CONTACT_PT = 20;   // 10pt

// 240 twips is single spacing, so 1.15 is 276.
const LINE = 276;

// Vertical rhythm, straight off the base CV.
const AFTER_NAME = 40;
const AFTER_CONTACT = 160;
const BEFORE_HEADING = 200;
const AFTER_HEADING = 100;
const AFTER_SUMMARY = 80;
const BEFORE_ENTRY = 100;
const AFTER_ENTRY_HEAD = 0;
const AFTER_ROLE_LINE = 60;
const AFTER_BULLET = 60;
const BEFORE_SKILLS = 60;

// The base CV's bullet: indented 0.2", hanging 0.125".
const BULLET_INDENT = 288;
const BULLET_HANGING = 180;

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
const ACCENT = ((spec.theme && spec.theme.accent) || "117A82").replace(/^#/, "");
const SEP = spec.contact_separator || "  |  ";

const txt = (v) => String(v === undefined || v === null ? "" : v);
const has = (v) => txt(v).trim().length > 0;

/** A paragraph with the house line spacing, unless told otherwise. */
function para(opts) {
  return new Paragraph({ spacing: { line: LINE, lineRule: "auto" }, ...opts });
}

function run(text, opts = {}) {
  return new TextRun({ text: txt(text), font: FONT, size: BODY_PT, ...opts });
}

/** The teal rule under a section heading. The role-title header takes the same style,
 *  which is what the skill means by "matching the style of section headers". */
const accentRule = {
  bottom: { style: BorderStyle.SINGLE, size: 6, color: ACCENT, space: 2 },
};

// ---------------------------------------------------------------- blocks

function nameHeader() {
  return para({
    alignment: AlignmentType.CENTER,
    spacing: { line: LINE, lineRule: "auto", after: AFTER_NAME },
    children: [run(spec.name, { size: NAME_PT, bold: true, color: ACCENT })],
  });
}

/** One centered 10pt line. Anything carrying an href becomes a real ExternalHyperlink,
 *  not blue text that does nothing when clicked. */
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

/** 13pt bold ALL CAPS, left, teal, bottom border. */
function heading(text, before = BEFORE_HEADING) {
  return para({
    border: accentRule,
    alignment: AlignmentType.LEFT,
    spacing: { line: LINE, lineRule: "auto", before, after: AFTER_HEADING },
    children: [run(txt(text).toUpperCase(), {
      size: HEADING_PT, bold: true, color: ACCENT,
    })],
  });
}

/** left [right-tab] right. The tab stop is what puts dates and locations on the right
 *  margin; without it they sit wherever the text happens to end. Both runs carry the same
 *  emphasis, matching the base CV: the company line is bold throughout, the role line
 *  italic throughout. */
function tabbedLine(left, right, emphasis, before, after) {
  const children = [run(left, emphasis)];
  if (has(right)) children.push(run(`\t${txt(right)}`, emphasis));
  return para({
    tabStops: [{ type: TabStopType.RIGHT, position: RIGHT_TAB }],
    spacing: { line: LINE, lineRule: "auto", before, after },
    children,
  });
}

/** A bullet. `item` is a string, or {lead, text} where the lead-in is bold -- which is how
 *  every project reads on the base CV. */
function bullet(item) {
  const children = [];
  if (item && typeof item === "object") {
    if (has(item.lead)) children.push(run(`${txt(item.lead).trim()} `, { bold: true }));
    if (has(item.text)) children.push(run(txt(item.text).trim()));
  } else {
    children.push(run(item));
  }
  return para({
    numbering: { reference: "cv-bullets", level: 0 },
    spacing: { line: LINE, lineRule: "auto", after: AFTER_BULLET },
    children,
  });
}

function bulletText(item) {
  if (item && typeof item === "object") {
    return `${txt(item.lead).trim()} ${txt(item.text).trim()}`.trim();
  }
  return txt(item).trim();
}

/** One employer, school or other dated thing. An entry has one header line and one or
 *  more roles under it: LexisNexis is a single company with two titles, and rendering it
 *  as two companies would read as two employers. */
function entryBlock(entry) {
  const out = [];
  const roles = (entry.roles || []).filter(
    (r) => has(r.sub_left) || has(r.sub_right) || (r.bullets || []).some(bulletText));
  if (!has(entry.left) && !has(entry.right) && !roles.length) return out;

  if (has(entry.left) || has(entry.right)) {
    out.push(tabbedLine(entry.left, entry.right, { bold: true },
                        BEFORE_ENTRY, AFTER_ENTRY_HEAD));
  }
  roles.forEach((role) => {
    if (has(role.sub_left) || has(role.sub_right)) {
      // No leading gap, on the first role or any later one. The base CV runs a second
      // title straight on from the previous role's last bullet, and a gap there reads as
      // a new employer rather than a promotion inside the same one.
      out.push(tabbedLine(role.sub_left, role.sub_right, { italics: true },
                          0, AFTER_ROLE_LINE));
    }
    (role.bullets || []).filter(bulletText).forEach((b) => out.push(bullet(b)));
  });
  return out;
}

// ---------------------------------------------------------------- document

const body = [nameHeader(), contactLine()];

if (has(spec.role_title)) body.push(heading(spec.role_title, 0));
if (has(spec.summary)) {
  body.push(para({
    spacing: { line: LINE, lineRule: "auto", after: AFTER_SUMMARY },
    children: [run(spec.summary)],
  }));
}

(spec.sections || []).forEach((section) => {
  const blocks = [];
  if (section.kind === "bullets") {
    (section.bullets || []).filter(bulletText).forEach((b) => blocks.push(bullet(b)));
  } else {
    (section.entries || []).forEach((e) => entryBlock(e).forEach((p) => blocks.push(p)));
  }
  if (!blocks.length) return;
  body.push(heading(section.heading));
  blocks.forEach((p) => body.push(p));
});

if (has(spec.skills)) {
  body.push(heading("Skills"));
  body.push(para({
    alignment: AlignmentType.CENTER,
    spacing: { line: LINE, lineRule: "auto", before: BEFORE_SKILLS },
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
          paragraph: { indent: { left: BULLET_INDENT, hanging: BULLET_HANGING } },
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
