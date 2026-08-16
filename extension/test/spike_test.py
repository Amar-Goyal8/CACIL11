from playwright.sync_api import sync_playwright
import pathlib, json, sys

HERE = pathlib.Path(__file__).resolve().parent
JS = (HERE.parent / "content.js").read_text()
CSS = (HERE.parent / "highlight.css").read_text()

fails = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  :: " + str(detail)) if detail else ""))
    if not cond:
        fails.append(name)

with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 900, "height": 800})
    pg.goto("file://" + str(HERE / "fixture.html"), wait_until="load")
    pg.add_style_tag(content=CSS)
    logs = []
    pg.on("console", lambda m: logs.append(m.text))
    pg.evaluate(JS)
    pg.wait_for_timeout(400)

    print("\n-- console --")
    for l in logs: print("   ", l)

    print("\n-- structural --")
    root = pg.evaluate("() => __spike.findArticleRoot()?.id")
    check("article root is #story", root == "story", root)

    txt = pg.evaluate("() => __spike.index().text")
    check("nav text excluded", "Sections: World" not in txt)
    check("aside text excluded", "Related coverage" not in txt)
    check("footer text excluded", "Copyright notice" not in txt)
    check("no word fusion across paragraphs", "warm.In a report" not in txt and "warm.\n\nIn a report" in txt)

    print("\n-- baseline anchoring --")
    res = pg.evaluate("""() => __spike.score('test') && __spike.targets.map((t,i) => {
      const at = __spike.findQuote(__spike.index(), t);
      const rs = at ? __spike.rangesFor(at.start, at.end, __spike.index()) : null;
      const txt = rs ? rs.map(x => x.toString()).join('') : '';
      const rects = rs ? rs.flatMap(x => [...x.getClientRects()]) : [];
      return { crossings: t.crossings, resolved: !!at,
               correct: rs ? txt.replace(/\\s+/g,' ').trim() === t.exact.replace(/\\s+/g,' ').trim() : false,
               visible: rects.some(x => x.width>0 && x.height>0) };
    })""")
    for i, r in enumerate(res):
        check(f"target {i+1} resolved+correct+visible (crosses {r['crossings']} nodes)",
              r["resolved"] and r["correct"] and r["visible"], r)
    check("at least one target crosses an inline element",
          any(r["crossings"] > 1 for r in res), [r["crossings"] for r in res])

    print("\n-- hand-written quote, crossing an <a> --")
    hard = pg.evaluate("""() => {
      const idx = __spike.index();
      const t = { exact: "the first formal sitting since talks stalled last autumn",
                  prefix: "nuclear program, ", suffix: ". The mood" };
      const at = __spike.findQuote(idx, t);
      if (!at) return { found: false };
      const rs = __spike.rangesFor(at.start, at.end, idx);
      return { found: true, text: rs.map(x => x.toString()).join(''),
               startTag: rs[0].startContainer.parentElement.tagName,
               endTag: rs[rs.length-1].endContainer.parentElement.tagName,
               rects: rs.flatMap(x => [...x.getClientRects()]).length };
    }""")
    check("quote spanning a link resolves", hard.get("found"), hard)
    # The DOM copy carries the source line break. Compare collapsed, same as content.js does.
    collapsed = " ".join((hard.get("text") or "").split())
    check("resolved text is the quote, whitespace aside",
          collapsed == "the first formal sitting since talks stalled last autumn", collapsed)
    check("range starts inside the <a>", hard.get("startTag") == "A", hard)

    print("\n-- whitespace tolerance (nbsp + newline in DOM) --")
    ws = pg.evaluate("""() => {
      const idx = __spike.index();
      // Single-spaced, plain space, exactly how a backend would hand it back.
      const t = { exact: "Iran's enriched uranium stockpile reached roughly 6,200 kilograms in May",
                  prefix: "noted that ", suffix: ", a figure" };
      const at = __spike.findQuote(idx, t);
      if (!at) return { found: false };
      const rs = __spike.rangesFor(at.start, at.end, idx);
      return { found: true, text: rs.map(x => x.toString()).join(''), tag: rs[0].startContainer.parentElement.tagName };
    }""")
    check("nbsp/newline quote still resolves", ws.get("found"), ws)
    check("lands inside the <strong>", ws.get("tag") == "STRONG", ws)

    print("\n-- highlights actually registered --")
    hl = pg.evaluate("() => [...CSS.highlights.keys()]")
    check("CSS.highlights populated", len(hl) > 0, hl)
    painted = pg.evaluate("() => { let n=0; for (const h of CSS.highlights.values()) n += h.size; return n; }")
    check("every target painted", painted >= len(res), f"{painted} ranges for {len(res)} targets")

    print("\n-- targets sit at the top, one per paragraph --")
    paras = pg.evaluate("""() => __spike.results().map(r =>
      r.ranges[0].startContainer.parentElement.closest('p') === null ? null
        : [...document.querySelectorAll('#story p')].indexOf(r.ranges[0].startContainer.parentElement.closest('p')))""")
    check("one target per paragraph", len(set(paras)) == len(paras), paras)
    check("all targets inside the first four paragraphs", max(paras) <= 3, paras)
    cats = pg.evaluate("() => __spike.results().map(r => r.cat)")
    check("colours run green, yellow, red", cats == ["corrob", "context", "contested"], cats)

    print("\n-- hover fill --")
    rest_keys = pg.evaluate("() => [...CSS.highlights.keys()]")
    check("no fill registry at rest", "cl-hover" not in rest_keys, rest_keys)

    pt = pg.evaluate("""() => {
      const r0 = __spike.results()[0];
      const b = __spike.linesOf(r0.ranges, r0.glyph)[0];
      return { x: Math.round((b.left + b.right) / 2), y: Math.round((b.top + b.bottom) / 2), cat: r0.cat };
    }""")
    sizes_before = pg.evaluate("() => __spike.results().map(r => CSS.highlights.get('cl-' + r.cat).size)")

    pg.mouse.move(pt["x"], pt["y"])
    pg.wait_for_timeout(40)
    pg.screenshot(path=str(HERE / "_early.png"))
    pg.wait_for_timeout(400)
    pg.screenshot(path=str(HERE / "_late.png"))

    check("fill registry appears on hover",
          "cl-hover" in pg.evaluate("() => [...CSS.highlights.keys()]"))
    sizes_after = pg.evaluate("() => __spike.results().map(r => CSS.highlights.get('cl-' + r.cat).size)")
    check("underlines stay put while hovering", sizes_before == sizes_after,
          f"{sizes_before} then {sizes_after}")

    from PIL import Image
    def sample(name):
        im = Image.open(HERE / name).convert("RGB")
        return im.getpixel((pt["x"], pt["y"] + 6))
    early, late = sample("_early.png"), sample("_late.png")
    check("fill animates rather than snapping", early != late, f"{early} then {late}")
    check("fill settles on the category tint", late == (226, 240, 231), late)

    pg.mouse.move(4, 4)
    pg.wait_for_timeout(400)
    check("fill clears when the cursor leaves",
          "cl-hover" not in pg.evaluate("() => [...CSS.highlights.keys()]"))

    print("\n-- hit test hugs the text --")
    probe = pg.evaluate("""() => {
      const rs = __spike.results();
      const boxes = __spike.linesOf(rs[0].ranges, rs[0].glyph);
      const first = boxes[0], last = boxes[boxes.length - 1];
      const at = (x, y) => { const h = __spike.hitTest(x, y, rs); return h === rs[0]; };
      return {
        centre:      at((first.left + first.right) / 2, (first.top + first.bottom) / 2),
        leftEdge:    at(first.left + 1, (first.top + first.bottom) / 2),
        rightEdge:   at(last.right - 1, (last.top + last.bottom) / 2),
        gutter:      at(last.right + 40, (last.top + last.bottom) / 2),
        farRight:    at(last.right + 300, (last.top + last.bottom) / 2),
        above:       at((first.left + first.right) / 2, first.top - 10),
        below:       at((last.left + last.right) / 2, last.bottom + 10),
        lines:       boxes.length
      };
    }""")
    check("passage wraps across more than one line", probe["lines"] > 1, probe["lines"])
    check("hit on the centre of the text", probe["centre"], probe)
    check("hit at the left edge", probe["leftEdge"], probe)
    check("hit at the right edge", probe["rightEdge"], probe)
    check("no hit in the gutter past the line", not probe["gutter"], probe)
    check("no hit far to the right", not probe["farRight"], probe)
    check("no hit above the line", not probe["above"], probe)
    check("no hit below the line", not probe["below"], probe)

    print("\n-- gaps inside a wrapped passage stay live --")
    def gap_probe():
        return pg.evaluate("""() => {
          const rs = __spike.results();
          const lines = r => __spike.linesOf(r.ranges, r.glyph);
          // Whichever passage wraps across the most lines exercises the gaps best.
          const target = rs.reduce((a, b) => (lines(b).length > lines(a).length ? b : a));
          const ls = lines(target);
          const at = (x, y) => __spike.hitTest(x, y, rs) === target;

          const out = { lines: ls.length, gaps: [] };
          for (let i = 0; i < ls.length - 1; i++) {
            const y = (ls[i].bottom + ls[i + 1].top) / 2;
            const x = Math.max(ls[i].left, ls[i + 1].left) + 20;
            out.gaps.push(at(x, y));
          }
          const last = ls[ls.length - 1];
          out.above = at(ls[0].left + 20, ls[0].top - 8);
          out.below = at(last.left + 20, last.bottom + 8);
          out.pastLastLine = at(last.right + 60, (last.top + last.bottom) / 2);
          return out;
        }""")

    g = gap_probe()
    check("passage wraps across at least two lines", g["lines"] >= 2, g)
    check("gap between the lines is live", all(g["gaps"]), g)
    check("still no hit above the first line", not g["above"], g)
    check("still no hit below the last line", not g["below"], g)
    check("no hit past the end of the last line", not g["pastLastLine"], g)

    pg.set_viewport_size({"width": 380, "height": 800})
    pg.wait_for_timeout(600)
    pg.evaluate("() => __spike.score('narrow')")
    g3 = gap_probe()
    check("passage spans three or more lines when narrow", g3["lines"] >= 3, g3)
    check("every internal gap is live", all(g3["gaps"]), g3)
    check("no hit above the first line when narrow", not g3["above"], g3)
    check("no hit below the last line when narrow", not g3["below"], g3)
    check("no hit past the end of the last line when narrow", not g3["pastLastLine"], g3)

    pg.set_viewport_size({"width": 900, "height": 800})
    pg.wait_for_timeout(600)
    pg.evaluate("() => __spike.score('back to wide')")

    print("\n-- survives lazy load --")
    pg.evaluate("""() => {
      const p = document.createElement('p');
      p.textContent = 'A late paragraph appended after render, exactly how infinite scroll behaves on a real news site today.';
      document.querySelector('#story').appendChild(p);
    }""")
    pg.wait_for_timeout(700)
    after = pg.evaluate("() => __spike.score('after lazy load')")
    check("all targets still anchored after DOM append", after == len(res), f"{after}/{len(res)}")

    print("\n-- survives reflow --")
    pg.set_viewport_size({"width": 420, "height": 800})
    pg.wait_for_timeout(700)
    reflow = pg.evaluate("() => __spike.score('after resize')")
    check("all targets still anchored after resize", reflow == len(res), f"{reflow}/{len(res)}")

    print("\n-- an ad injected mid-passage --")
    pg.set_viewport_size({"width": 900, "height": 800})
    pg.wait_for_timeout(300)
    pg.evaluate("() => __spike.score('reset')")
    before = pg.evaluate("() => __spike.results().map(r => r.why)")

    pg.evaluate("""() => {
      // Drop an ad inside the second passage, between two of its text nodes,
      // exactly where sites place them.
      const strong = document.querySelector('#story strong');
      const ad = document.createElement('aside');
      ad.textContent = ' ADVERTISEMENT Subscribe today for unlimited access ';
      strong.after(ad);
    }""")
    pg.wait_for_timeout(700)
    after = pg.evaluate("() => __spike.results().map(r => r.why)")
    check("ad text stays out of the flat index",
          "ADVERTISEMENT" not in pg.evaluate("() => __spike.index().text"))
    check("passage wrapping the ad still reads correct", after == before, f"{before} then {after}")
    check("the ad's own words are not highlighted",
          pg.evaluate("""() => __spike.results().every(r =>
            !r.ranges || !r.ranges.some(x => x.toString().includes('ADVERTISEMENT')))"""))

    # Prove the test bites. Rebuild the old single-range behaviour by hand and
    # confirm it would have swallowed the ad.
    would_break = pg.evaluate("""() => {
      const r = __spike.results().find(x => x.ranges && x.ranges.length > 1);
      if (!r) return 'no multi-node passage';
      const first = r.ranges[0], last = r.ranges[r.ranges.length - 1];
      const single = document.createRange();
      single.setStart(first.startContainer, first.startOffset);
      single.setEnd(last.endContainer, last.endOffset);
      return single.toString().includes('ADVERTISEMENT');
    }""")
    check("one range across the gap would have swallowed the ad", would_break is True, would_break)
    pg.evaluate("() => document.querySelector('#story aside').remove()")
    pg.wait_for_timeout(600)

    print("\n-- a paywall stops the scan --")
    WALL = "() => { const n = __spike.index().text.length; return __spike.paywallEngaged(__spikeProbe.findArticleRoot(), n, n); }"
    check("no wall on a clean page", pg.evaluate(WALL) is None)

    pg.evaluate("""() => {
      const wall = document.createElement('div');
      wall.style.cssText = 'position:fixed;left:0;right:0;bottom:0;height:40vh;background:#fff;z-index:99';
      wall.textContent = 'Subscribe to continue reading';
      document.body.appendChild(wall);
    }""")
    pg.wait_for_timeout(200)
    reason = pg.evaluate(WALL)
    check("overlay covering the article is detected", reason is not None, reason)
    check("scan stops and reports paywalled", pg.evaluate("() => __spike.score('wall')") == -1)
    check("highlights are cleared when the wall is up",
          pg.evaluate("() => [...CSS.highlights.keys()].length") == 0)

    pg.evaluate("() => document.querySelector('div[style*=\"40vh\"]').remove()")
    pg.wait_for_timeout(200)
    check("scan resumes once the wall goes",
          pg.evaluate("() => __spike.score('wall gone')") == 3)

    check("scroll lock alone counts as a wall", pg.evaluate("""() => {
      document.body.style.overflow = 'hidden';
      const n = __spike.index().text.length;
      const r = __spike.paywallEngaged(__spikeProbe.findArticleRoot(), n, n);
      document.body.style.overflow = '';
      return r !== null;
    }"""))

    # NYT's actual behaviour: no CSS trick, no scroll lock, the words just go.
    check("text quietly disappearing counts as a wall", pg.evaluate("""() => {
      const n = __spike.index().text.length;
      return __spike.paywallEngaged(__spikeProbe.findArticleRoot(), Math.round(n * 0.3), n) !== null;
    }"""))
    check("a small trim does not", pg.evaluate("""() => {
      const n = __spike.index().text.length;
      return __spike.paywallEngaged(__spikeProbe.findArticleRoot(), Math.round(n * 0.95), n) === null;
    }"""))

    ps = pg.evaluate("() => document.querySelectorAll('#story p').length")
    pg.evaluate("""() => {
      // Delete the tail of the article, the way NYT does ten seconds in.
      const paras = [...document.querySelectorAll('#story p')];
      paras.slice(2).forEach(p => p.remove());
    }""")
    pg.wait_for_timeout(700)
    check("live truncation stops the scan", pg.evaluate("() => __spike.score('truncated')") == -1,
          f"was {ps} paragraphs")
    check("highlights cleared after truncation",
          pg.evaluate("() => [...CSS.highlights.keys()].length") == 0)

    # That test deleted the article, so start clean for what follows.
    pg.goto("file://" + str(HERE / "fixture.html"), wait_until="load")
    pg.add_style_tag(content=CSS)
    pg.evaluate(JS)
    pg.wait_for_timeout(400)

    print("\n-- fill adapts to a dark page --")
    pg.set_viewport_size({"width": 900, "height": 800})
    pg.wait_for_timeout(400)

    READ_FILL = """() => {
      for (const s of document.styleSheets) {
        let rules; try { rules = [...s.cssRules]; } catch (e) { continue; }
        for (const r of rules) if (r.selectorText === '::highlight(cl-hover)') return r.style.backgroundColor;
      }
      return null;
    }"""

    def relative_luminance(c):
        def channel(v):
            v /= 255
            return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
        return 0.2126 * channel(c[0]) + 0.7152 * channel(c[1]) + 0.0722 * channel(c[2])

    def contrast(a, b):
        hi, lo = sorted([relative_luminance(a), relative_luminance(b)], reverse=True)
        return (hi + 0.05) / (lo + 0.05)

    def read_fill():
        import re as _re
        raw = pg.evaluate(READ_FILL) or ""
        nums = [float(n) for n in _re.findall(r"[\d.]+", raw)]
        return tuple(int(n) for n in nums[:3])

    def hover_first():
        pt = pg.evaluate("""() => {
          const r = __spike.results()[0];
          const l = __spike.linesOf(r.ranges, r.glyph)[0];
          return { x: Math.round(l.left + 40), y: Math.round((l.top + l.bottom) / 2) };
        }""")
        pg.mouse.move(pt["x"], pt["y"])
        pg.wait_for_timeout(320)

    check("light page reads as light",
          pg.evaluate("() => __spike.pageIsDark(document.querySelector('#story'))") is False)
    hover_first()
    light_fill = read_fill()
    check("light page uses the pale tint", light_fill == (226, 240, 231), light_fill)
    check("pale tint clears 4.5:1 against dark text",
          contrast(light_fill, (17, 17, 17)) >= 4.5, round(contrast(light_fill, (17, 17, 17)), 2))
    pg.mouse.move(4, 4); pg.wait_for_timeout(250)

    pg.add_style_tag(content="body{background:#0f0f0f !important;color:#e8e8e8 !important}")
    pg.wait_for_timeout(200)
    pg.evaluate("() => __spike.score('theme flip')")
    check("dark page reads as dark",
          pg.evaluate("() => __spike.pageIsDark(document.querySelector('#story'))") is True)
    hover_first()
    dark_fill = read_fill()
    check("dark page swaps to the deep tint", dark_fill == (28, 61, 43), dark_fill)
    check("deep tint clears 4.5:1 against light text",
          contrast(dark_fill, (232, 232, 232)) >= 4.5, round(contrast(dark_fill, (232, 232, 232)), 2))

    pg.screenshot(path=str(HERE / "spike.png"), full_page=True)

    print("\n-- a section front is refused --")
    sec = b.new_page(viewport={"width": 900, "height": 800})
    seclogs = []
    sec.on("console", lambda m: seclogs.append(m.text))
    sec.goto("file://" + str(HERE / "fixture_section.html"), wait_until="load")
    sec.add_style_tag(content=CSS)
    sec.evaluate(JS)
    sec.wait_for_timeout(400)

    check("structure rejects the listing even after a detection signal fires",
          sec.evaluate("() => __spikeProbe.findArticleRoot()") is None,
          "detected as: " + str(sec.evaluate("() => __spikeProbe.isArticlePage()")))
    check("the listing is recognised as one",
          sec.evaluate("""() => __spikeProbe.looksLikeListing(
            [...document.querySelectorAll('p')].filter(p => p.textContent.trim().length >= 60))"""))
    check("nothing painted on a section front",
          sec.evaluate("() => [...CSS.highlights.keys()].length") == 0)
    check("no badge on a section front",
          sec.evaluate("() => document.querySelectorAll('div[style*=\"2147483647\"]').length") == 0)

    # strip the meta so the structural check has to carry it alone
    sec.evaluate("() => document.querySelector('meta[property=\"og:type\"]').remove()")
    check("without og:type, card structure still gives it away",
          sec.evaluate("() => __spikeProbe.findArticleRoot()") is None)
    check("the article fixture is still accepted",
          pg.evaluate("() => __spike.findArticleRoot() !== null"))

    b.close()

print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}"))
sys.exit(1 if fails else 0)
