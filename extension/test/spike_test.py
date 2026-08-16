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
      const r  = at ? __spike.rangeFor(at.start, at.end, __spike.index()) : null;
      return { crossings: t.crossings, resolved: !!at,
               correct: r ? r.toString().replace(/\\s+/g,' ').trim() === t.exact.replace(/\\s+/g,' ').trim() : false,
               visible: r ? [...r.getClientRects()].some(x => x.width>0 && x.height>0) : false };
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
      const r = __spike.rangeFor(at.start, at.end, idx);
      return { found: true, text: r.toString(),
               startTag: r.startContainer.parentElement.tagName,
               endTag: r.endContainer.parentElement.tagName,
               rects: [...r.getClientRects()].length };
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
      const r = __spike.rangeFor(at.start, at.end, idx);
      return { found: true, text: r.toString(), tag: r.startContainer.parentElement.tagName };
    }""")
    check("nbsp/newline quote still resolves", ws.get("found"), ws)
    check("lands inside the <strong>", ws.get("tag") == "STRONG", ws)

    print("\n-- highlights actually registered --")
    hl = pg.evaluate("() => [...CSS.highlights.keys()]")
    check("CSS.highlights populated", len(hl) > 0, hl)
    painted = pg.evaluate("() => { let n=0; for (const h of CSS.highlights.values()) n += h.size; return n; }")
    check("every target painted", painted == len(res), f"{painted} ranges for {len(res)} targets")

    print("\n-- targets sit at the top, one per paragraph --")
    paras = pg.evaluate("""() => __spike.results().map(r =>
      r.range.startContainer.parentElement.closest('p') === null ? null
        : [...document.querySelectorAll('#story p')].indexOf(r.range.startContainer.parentElement.closest('p')))""")
    check("one target per paragraph", len(set(paras)) == len(paras), paras)
    check("all targets inside the first four paragraphs", max(paras) <= 3, paras)
    cats = pg.evaluate("() => __spike.results().map(r => r.cat)")
    check("colours run green, yellow, red", cats == ["corrob", "context", "contested"], cats)

    print("\n-- hover fill --")
    rest_keys = pg.evaluate("() => [...CSS.highlights.keys()]")
    check("no fill registry at rest", "cl-hover" not in rest_keys, rest_keys)

    pt = pg.evaluate("""() => {
      const r0 = __spike.results()[0];
      const b = [...r0.range.getClientRects()][0];
      return { x: Math.round(b.x + b.width / 2), y: Math.round(b.y + b.height / 2), cat: r0.cat };
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
      const boxes = [...rs[0].range.getClientRects()];
      const first = boxes[0], last = boxes[boxes.length - 1];
      const at = (x, y) => { const h = __spike.hitTest(x, y, rs); return h === rs[0]; };
      return {
        centre:      at(first.x + first.width / 2, first.y + first.height / 2),
        leftEdge:    at(first.left + 1, first.y + first.height / 2),
        rightEdge:   at(last.right - 1, last.y + last.height / 2),
        gutter:      at(last.right + 40, last.y + last.height / 2),
        farRight:    at(last.right + 300, last.y + last.height / 2),
        above:       at(first.x + first.width / 2, first.top - 10),
        below:       at(last.x + last.width / 2, last.bottom + 10),
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
          const lines = r => __spike.linesOf(r.range, r.glyph);
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

    pg.screenshot(path=str(HERE / "spike.png"), full_page=True)
    b.close()

print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}"))
sys.exit(1 if fails else 0)
