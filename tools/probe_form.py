#!/usr/bin/env python3
"""Dump the structure of an application form, so a driver can be written against what is
actually on the page rather than what it was assumed to look like.

    python tools/probe_form.py <url>            print every control, its label and its kind
    python tools/probe_form.py <url> --html     also print the markup around each control

This reads. It fills nothing, clicks nothing and submits nothing: there is no code path in
here that touches a button, because the pages it is pointed at are real employers' forms.

It exists because the Greenhouse driver was written against a saved copy of a real
Greenhouse form and the selectors were right first time, and the next board's driver
deserves the same. Boards that render their form in JavaScript -- Ashby, most of them now
-- give a plain fetch nothing but a spinner, so this needs a browser and therefore a
machine with real network access. In practice that means the runner: see the `probe` input
on .github/workflows/form-smoke.yml.
"""

import json
import sys

sys.path.insert(0, __file__.rsplit("/", 2)[0])
import submit  # noqa: E402

# Everything that might identify a control, because on a hydrated single-page app the
# useful handle is rarely the id: Ashby labels its fields with aria-labelledby and hangs
# data-testid off the wrappers, and which of those is stable is exactly what this answers.
DUMP_JS = r"""
() => {
  const near = (el) => {
    let n = el, hops = 0;
    while (n && hops < 4) {
      const t = (n.innerText || '').trim();
      if (t && t.length < 200) return t.split('\n').slice(0, 3).join(' / ');
      n = n.parentElement; hops++;
    }
    return '';
  };
  const controls = [...document.querySelectorAll('input, select, textarea, [role=combobox], [contenteditable=true]')]
    .map((el) => ({
      tag: el.tagName,
      type: (el.type || '').toLowerCase(),
      id: el.id || '',
      name: el.name || '',
      testid: el.getAttribute('data-testid') || '',
      role: el.getAttribute('role') || '',
      required: !!el.required || el.getAttribute('aria-required') === 'true',
      ariaLabel: el.getAttribute('aria-label') || '',
      labelledBy: el.getAttribute('aria-labelledby') || '',
      labelText: (el.labels && el.labels[0] ? el.labels[0].innerText : '').trim(),
      placeholder: el.placeholder || '',
      options: el.tagName === 'SELECT'
        ? [...el.options].map((o) => (o.text || '').trim()) : [],
      context: near(el),
      hidden: !(el.offsetWidth || el.offsetHeight || el.getClientRects().length),
    }));
  const buttons = [...document.querySelectorAll('button, [role=button], input[type=submit]')]
    .map((b) => ({text: (b.innerText || b.value || '').trim().slice(0, 60),
                  type: b.getAttribute('type') || '',
                  testid: b.getAttribute('data-testid') || '',
                  id: b.id || ''}))
    .filter((b) => b.text || b.testid);
  const testids = [...document.querySelectorAll('[data-testid]')]
    .map((e) => e.getAttribute('data-testid'));
  return {
    url: location.href,
    title: document.title,
    controls,
    buttons,
    testids: [...new Set(testids)].slice(0, 60),
    // Anti-automation is worth knowing about before a driver is written, not after a
    // submission silently fails.
    recaptcha: /recaptcha|hcaptcha|turnstile/i.test(document.documentElement.innerHTML),
    iframes: [...document.querySelectorAll('iframe')].map((f) => f.src).slice(0, 10),
  };
}
"""


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    url = sys.argv[1]
    submit.ensure_browser(quiet=True)
    with submit.Session() as s:
        s.page.goto(url, wait_until="domcontentloaded",
                    timeout=submit.BROWSER_TIMEOUT_MS)
        # A single-page app needs longer than a rendered form: the shell arrives first and
        # the fields are fetched after it.
        try:
            s.page.wait_for_selector("input, textarea, [role=combobox]",
                                     timeout=submit.BROWSER_TIMEOUT_MS)
        except Exception:
            print("  no control appeared; dumping whatever is there")
        s.page.wait_for_timeout(4000)
        data = s.page.evaluate(DUMP_JS)

        print(f"\n=== {data['title']}\n=== {data['url']}")
        print(f"=== anti-automation present: {data['recaptcha']}")
        if data["iframes"]:
            print("=== iframes: " + ", ".join(f or "(inline)" for f in data["iframes"]))
        print(f"\n--- {len(data['controls'])} controls")
        for c in data["controls"]:
            print(json.dumps(c, ensure_ascii=False))
        print(f"\n--- buttons")
        for b in data["buttons"]:
            print(json.dumps(b, ensure_ascii=False))
        print(f"\n--- data-testid values on the page")
        print(", ".join(data["testids"]))

        if "--html" in sys.argv:
            print("\n--- markup around the first ten controls")
            for c in data["controls"][:10]:
                sel = (f"#{submit.css_id(c['id'])}" if c["id"]
                       else f"[data-testid='{c['testid']}']" if c["testid"] else "")
                if not sel:
                    continue
                try:
                    html = s.page.eval_on_selector(
                        sel, "el => (el.closest('div,fieldset,label') || el).outerHTML")
                    print(f"\n### {sel}\n{html[:1200]}")
                except Exception as e:
                    print(f"\n### {sel} -- could not read: {str(e)[:80]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
