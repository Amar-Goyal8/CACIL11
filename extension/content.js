// ClearLens highlight spike
//
// One question: do exact passages stay marked on a live news page?
// The harness at the bottom gets deleted. buildTextIndex, findQuote and
// rangeFor move into the real content script, so those three are written
// for keeps.

const SKIP = new Set([
  'SCRIPT', 'STYLE', 'NOSCRIPT', 'SVG', 'NAV', 'ASIDE',
  'HEADER', 'FOOTER', 'FORM', 'BUTTON', 'IFRAME', 'FIGCAPTION'
]);

const BLOCK = new Set([
  'P', 'DIV', 'SECTION', 'ARTICLE', 'LI', 'BLOCKQUOTE',
  'H1', 'H2', 'H3', 'H4', 'H5', 'H6', 'UL', 'OL', 'TD', 'TR'
]);

const CATS = ['corrob', 'context', 'contested', 'alone'];

function collapse(s) {
  return s.replace(/\s+/g, ' ').trim();
}

function isArticlePage() {
  for (const el of document.querySelectorAll('script[type="application/ld+json"]')) {
    try {
      const data = JSON.parse(el.textContent);
      const nodes = [].concat(data, data['@graph'] || []);
      for (const n of nodes) {
        const types = [].concat((n && n['@type']) || []);
        if (types.some(t => /NewsArticle|ReportageNewsArticle|Article/.test(t))) return 'jsonld';
      }
    } catch (e) {
      // Plenty of sites ship malformed JSON-LD. Skip the block and keep looking.
    }
  }
  if (document.querySelector('meta[property="og:type"][content="article"]')) return 'og';
  if (document.querySelector('article')) return 'article-tag';
  return null;
}

// Readability hands back a cleaned copy of the article, so character offsets
// into its output point at nodes nobody rendered. Highlights need the live
// tree. Walk the real paragraphs and take their common ancestor instead.
function findArticleRoot() {
  const paras = [...document.querySelectorAll('p')].filter(p => {
    if (p.textContent.trim().length < 60) return false;
    for (let n = p; n && n !== document.body; n = n.parentElement) {
      if (SKIP.has(n.tagName)) return false;
    }
    return true;
  });

  if (paras.length < 3) return null;

  let root = paras[0];
  for (const p of paras) {
    while (root && !root.contains(p)) root = root.parentElement;
  }
  return root;
}

function nearestBlock(node) {
  for (let n = node.parentElement; n; n = n.parentElement) {
    if (BLOCK.has(n.tagName)) return n;
  }
  return null;
}

// Flattens the article into one string and remembers which text node every
// character came from. The backend works in offsets into this string.
function buildTextIndex(root) {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      if (!node.textContent.trim()) return NodeFilter.FILTER_REJECT;
      for (let n = node.parentElement; n && n !== root.parentElement; n = n.parentElement) {
        if (SKIP.has(n.tagName)) return NodeFilter.FILTER_REJECT;
      }
      return NodeFilter.FILTER_ACCEPT;
    }
  });

  const spans = [];
  let text = '';
  let lastBlock = null;
  let node;

  while ((node = walker.nextNode())) {
    const block = nearestBlock(node);

    // Without a gap here the last word of one paragraph fuses with the first
    // word of the next: "rather than warm.In a report".
    if (lastBlock && block !== lastBlock) text += '\n\n';
    lastBlock = block;

    const raw = node.textContent;
    spans.push({ node, start: text.length, end: text.length + raw.length });
    text += raw;
  }

  return { root, text, spans };
}

function rangeFor(start, end, index) {
  const a = index.spans.find(s => start >= s.start && start < s.end);
  const b = index.spans.find(s => end > s.start && end <= s.end);
  if (!a || !b) return null;

  const range = document.createRange();
  range.setStart(a.node, start - a.start);
  range.setEnd(b.node, end - b.start);
  return range;
}

// Collapsed copy of the text plus a map from each collapsed character back to
// the raw offset, so a whitespace-tolerant match still resolves to real nodes.
function normalize(raw) {
  let text = '';
  const map = [];
  let prevSpace = true;

  for (let i = 0; i < raw.length; i++) {
    const space = /\s/.test(raw[i]);
    if (space && prevSpace) continue;
    text += space ? ' ' : raw[i];
    map.push(i);
    prevSpace = space;
  }

  if (text.endsWith(' ')) {
    text = text.slice(0, -1);
    map.pop();
  }
  return { text, map };
}

function allIndexes(haystack, needle) {
  const out = [];
  let i = haystack.indexOf(needle);
  while (i !== -1) {
    out.push(i);
    i = haystack.indexOf(needle, i + 1);
  }
  return out;
}

function tailMatch(a, b) {
  let n = 0;
  while (n < a.length && n < b.length && a[a.length - 1 - n] === b[b.length - 1 - n]) n++;
  return n;
}

function headMatch(a, b) {
  let n = 0;
  while (n < a.length && n < b.length && a[n] === b[n]) n++;
  return n;
}

// Short quotes repeat. Prefix and suffix pick the right occurrence.
function bestByContext(text, hits, target) {
  let best = hits[0];
  let bestScore = -1;

  for (const h of hits) {
    const before = text.slice(Math.max(0, h - 40), h);
    const after = text.slice(h + target.exact.length, h + target.exact.length + 40);
    const score = tailMatch(before, target.prefix || '') + headMatch(after, target.suffix || '');
    if (score > bestScore) {
      bestScore = score;
      best = h;
    }
  }
  return best;
}

function findQuote(index, target) {
  const hits = allIndexes(index.text, target.exact);
  if (hits.length === 1) {
    return { start: hits[0], end: hits[0] + target.exact.length };
  }
  if (hits.length > 1) {
    const at = bestByContext(index.text, hits, target);
    return { start: at, end: at + target.exact.length };
  }

  // Exact match failed. Line breaks and non-breaking spaces shift between the
  // text sent to the backend and the text sitting in the DOM later.
  const norm = normalize(index.text);
  const wanted = collapse(target.exact);
  const i = norm.text.indexOf(wanted);
  if (i === -1) return null;

  return { start: norm.map[i], end: norm.map[i + wanted.length - 1] + 1 };
}

const LINE = {
  corrob: '#3d8a5a',
  context: '#c9a227',
  contested: '#b8402f',
  alone: '#a8a8b2'
};

// Fill colours from the mock-up, as rgb so the fade animates the alpha.
const TINT = {
  corrob: [226, 240, 231],
  context: [247, 237, 210],
  contested: [251, 227, 222],
  alone: [236, 238, 242]
};

function setOrDrop(name, hl) {
  if (hl.size) CSS.highlights.set(name, hl);
  else CSS.highlights.delete(name);
}

// Resting registries hold every passage and never change on hover. The fill
// rides on top in its own registry at a higher priority, so the underline
// stays put underneath.
function paint(results) {
  if (!CSS.highlights) return false;

  for (const c of CATS) {
    const h = new Highlight();
    for (const r of results) {
      if (r.range && r.cat === c) h.add(r.range);
    }
    setOrDrop('cl-' + c, h);
  }
  return true;
}

// ::highlight() ignores CSS transitions, checked in Chromium. Fading means
// rewriting the rule every frame, so hold a handle on one. A page with a
// strict style-src leaves sheet null, and the fill lands instantly instead.
function makeHoverRule() {
  const el = document.createElement('style');
  el.textContent = '::highlight(cl-hover){background-color:rgba(0,0,0,0)}';
  document.head.appendChild(el);
  try {
    return el.sheet ? el.sheet.cssRules[0] : null;
  } catch (e) {
    return null;
  }
}

const PAD = 2;

// A line box is taller than the letters inside it, so testing the raw box
// lights a passage up from the blank space above and below. Trim to roughly
// the glyphs using the font size.
function glyphHeight(range) {
  const el = range.startContainer.parentElement;
  const size = el ? parseFloat(getComputedStyle(el).fontSize) : 16;
  return (size || 16) * 1.15;
}

// getClientRects gives one box per inline fragment, so a passage crossing a
// <strong> hands back three boxes sitting on the same line. Group them back
// into lines first. A range is contiguous, so one line means one span from
// its leftmost edge to its rightmost.
function linesOf(range, glyph) {
  const boxes = [...range.getClientRects()]
    .filter(b => b.width > 0 && b.height > 0)
    .sort((a, b) => a.top - b.top || a.left - b.left);

  const lines = [];
  for (const b of boxes) {
    const line = lines[lines.length - 1];
    const overlap = line ? Math.min(line.bottom, b.bottom) - Math.max(line.top, b.top) : 0;

    if (line && overlap > Math.min(b.height, line.bottom - line.top) / 2) {
      line.top = Math.min(line.top, b.top);
      line.bottom = Math.max(line.bottom, b.bottom);
      line.left = Math.min(line.left, b.left);
      line.right = Math.max(line.right, b.right);
    } else {
      lines.push({ top: b.top, bottom: b.bottom, left: b.left, right: b.right });
    }
  }

  // Trim the leading off the top of the first line and the bottom of the last,
  // nowhere else. Insetting every line leaves dead strips between them and the
  // fill drops out as the cursor crosses one. What remains follows the text:
  // a partial first line, full lines under it, a partial last line.
  for (let i = 0; i < lines.length; i++) {
    const l = lines[i];
    const lead = Math.max(0, l.bottom - l.top - glyph) / 2;
    l.hitTop = i === 0 ? l.top + lead : Math.min(l.top, (lines[i - 1].bottom + l.top) / 2);
    l.hitBottom = i === lines.length - 1
      ? l.bottom - lead
      : Math.max(l.bottom, (l.bottom + lines[i + 1].top) / 2);
  }
  return lines;
}

// Highlights are paint, not elements, so no mouse event ever fires on one.
// caretPositionFromPoint snaps to the nearest character and reports a hit from
// halfway across the page, so measure against the range's own geometry.
function hitTest(x, y, results) {
  for (const r of results) {
    if (!r.range) continue;
    for (const l of linesOf(r.range, r.glyph)) {
      if (y >= l.hitTop - PAD && y <= l.hitBottom + PAD
        && x >= l.left - PAD && x <= l.right + PAD) return r;
    }
  }
  return null;
}

// ---- harness ----------------------------------------------------------
// Everything below exists to grade the spike. None of this ships.

// Picks its own targets so the spike runs on any site with no per-site setup.
// One sentence from each of the first three paragraphs: green, yellow, red
// land near the top on separate lines, and one glance confirms all three.
//
// Earlier versions also targeted the last paragraph to stress lazy loading.
// Sites replace below-fold content constantly, so the score kept dropping for
// reasons unrelated to anchoring. Re-anchoring gets exercised by any DOM
// change anyway, wherever the targets sit.
function pickTargets(index) {
  const found = [];
  const seenPara = new Set();

  // Newlines run through a sentence whenever a site hard wraps its HTML
  // source inside a paragraph, so match across them and throw out anything
  // reaching past the paragraph break.
  const re = /[^.!?]{60,300}[.!?]/g;
  let m;

  while ((m = re.exec(index.text)) !== null && found.length < 3) {
    const exact = m[0].trim();
    if (exact.includes('\n\n')) continue;
    const start = index.text.indexOf(exact, m.index);
    if (start < 0) continue;

    const para = index.text.lastIndexOf('\n\n', start);
    if (seenPara.has(para)) continue;
    seenPara.add(para);

    const end = start + exact.length;
    found.push({
      exact,
      prefix: index.text.slice(Math.max(0, start - 32), start),
      suffix: index.text.slice(end, end + 32),
      crossings: index.spans.filter(s => s.start < end && s.end > start).length
    });
  }
  return found;
}

function check(index, targets) {
  return targets.map((t, i) => {
    const at = findQuote(index, t);
    const range = at ? rangeFor(at.start, at.end, index) : null;
    const rects = range ? [...range.getClientRects()] : [];

    const r = {
      n: i + 1,
      cat: CATS[i % CATS.length],
      quote: t.exact.slice(0, 55),
      crossings: t.crossings,
      resolved: !!at,
      correct: range ? collapse(range.toString()) === collapse(t.exact) : false,
      visible: rects.some(x => x.width > 0 && x.height > 0),
      glyph: range ? glyphHeight(range) : 0,
      range
    };

    // Name the failure. A count alone tells you nothing when you are ten
    // sites into a test run.
    r.why = !r.resolved ? 'not found'
      : !r.correct ? 'wrong text'
      : !r.visible ? 'no box'
      : 'ok';
    return r;
  });
}

function makeBadge() {
  const host = document.createElement('div');
  host.style.cssText = 'position:fixed;right:16px;bottom:16px;z-index:2147483647';

  // Closed root keeps the page's stylesheet out of the badge, and the page's
  // scripts from finding the badge.
  const shadow = host.attachShadow({ mode: 'closed' });

  const dots = document.createElement('div');
  dots.style.cssText = 'text-align:right;margin-bottom:4px';

  const box = document.createElement('div');
  box.style.cssText = [
    'font:12px/1.5 ui-monospace,Menlo,monospace',
    'color:#fff',
    'padding:8px 12px',
    'border-radius:6px',
    'cursor:pointer',
    'box-shadow:0 2px 12px rgba(0,0,0,.3)'
  ].join(';');

  shadow.appendChild(dots);
  shadow.appendChild(box);

  // documentElement, because some sites swap out body on navigation.
  document.documentElement.appendChild(host);
  return { box, dots };
}

// One square per passage, in its own colour, filled when anchored and hollow
// when broken. Faster to read than a fraction.
function drawDots(host, results) {
  host.textContent = '';
  for (const r of results) {
    const c = LINE[r.cat] || LINE.alone;
    const d = document.createElement('span');
    d.title = `${r.cat}: ${r.why}`;
    d.style.cssText = [
      'display:inline-block',
      'width:9px',
      'height:9px',
      'border-radius:2px',
      'margin-left:4px',
      `border:1px solid ${c}`,
      `background:${r.why === 'ok' ? c : 'transparent'}`
    ].join(';');
    host.appendChild(d);
  }
}

function run() {
  const kind = isArticlePage();
  if (!kind) {
    console.log('[spike] no article on this page, stopping');
    return;
  }

  const root = findArticleRoot();
  if (!root) {
    console.log('[spike] found no article body, stopping');
    return;
  }

  let index = buildTextIndex(root);
  const targets = pickTargets(index);
  if (!targets.length) {
    console.log('[spike] found no usable sentences, stopping');
    return;
  }

  const { box, dots } = makeBadge();
  let worst = Infinity;
  let lastUrl = location.href;
  let current = [];
  let hovered = null;

  const bare = rs => rs.map(({ range, ...r }) => r);

  const fill = new Highlight();
  fill.priority = 1;
  const rule = makeHoverRule();
  let alpha = 0;
  let frame = 0;

  function fade(cat, up) {
    cancelAnimationFrame(frame);
    const [r, g, b] = TINT[cat] || TINT.alone;

    if (!rule) {
      // No stylesheet handle, so no fade. Snapping beats nothing.
      return;
    }

    const from = alpha;
    const to = up ? 1 : 0;
    const began = performance.now();

    const step = now => {
      const t = Math.min(1, (now - began) / 140);
      alpha = from + (to - from) * (1 - Math.pow(1 - t, 3));
      rule.style.backgroundColor = `rgba(${r},${g},${b},${alpha.toFixed(3)})`;
      if (t < 1) frame = requestAnimationFrame(step);
    };
    frame = requestAnimationFrame(step);
  }

  function setHover(hit) {
    if (hit && hit.range) {
      fill.clear();
      fill.add(hit.range);
      CSS.highlights.set('cl-hover', fill);
      fade(hit.cat, true);
    } else {
      fade(null, false);
      setTimeout(() => {
        if (!hovered) CSS.highlights.delete('cl-hover');
      }, 160);
    }
  }

  function score(reason) {
    index = buildTextIndex(root.isConnected ? root : findArticleRoot() || root);
    const results = check(index, targets);
    const ok = results.filter(r => r.why === 'ok').length;

    current = results;
    hovered = null;
    paint(results);

    const broken = [...new Set(results.filter(r => r.why !== 'ok').map(r => r.why))];
    box.textContent = `spike ${ok}/${results.length} anchored`
      + (broken.length ? ` (${broken.join(', ')})` : '')
      + ` after ${reason}`;
    box.style.background = ok === results.length ? '#14532d' : '#7f1d1d';
    drawDots(dots, results);

    // Only shout about a regression, and never on the first pass.
    if (worst !== Infinity && ok < worst) {
      console.warn(`[spike] dropped to ${ok}/${results.length} after ${reason}`);
      console.table(bare(results));
    }
    worst = Math.min(worst, ok);
    box.onclick = () => console.table(bare(current));
    return ok;
  }

  let queued = false;
  addEventListener('mousemove', e => {
    if (queued) return;
    queued = true;
    requestAnimationFrame(() => {
      queued = false;
      const hit = hitTest(e.clientX, e.clientY, current);
      if (hit === hovered) return;
      hovered = hit;
      setHover(hit);
    });
  }, { passive: true });

  console.log(`[spike] ${location.hostname} detected via ${kind}, ${index.text.length} chars, ${targets.length} targets`);
  const first = score('load');
  console.log(`[spike] baseline ${first}/${targets.length}`);
  console.table(bare(current));

  let timer;
  const later = reason => {
    clearTimeout(timer);
    timer = setTimeout(() => score(reason), 350);
  };

  addEventListener('scroll', () => later('scroll'), { passive: true });
  addEventListener('resize', () => later('resize'));
  new MutationObserver(() => later('dom change')).observe(root, { childList: true, subtree: true });
  setTimeout(() => score('10s settle'), 10000);

  // No background worker here, so poll for the soft navigations React sites do.
  setInterval(() => {
    if (location.href === lastUrl) return;
    lastUrl = location.href;
    console.log('[spike] url changed, highlights below belong to the old article');
    score('soft navigation');
  }, 700);

  window.__spike = {
    index: () => index,
    results: () => current,
    targets, score, paint, hitTest, linesOf,
    buildTextIndex, findQuote, rangeFor, findArticleRoot
  };
}

run();
