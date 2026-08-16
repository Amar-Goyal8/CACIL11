# Highlight spike

Throwaway harness. Answers one question before we build anything else:

Do exact passages stay marked on a live news page?

If the answer is no, the in-page panel design does not work and ClearLens becomes a
side panel quoting passages instead of marking them in the article. Better to learn
this in August than in October.

## Run it

1. Open chrome://extensions
2. Turn on Developer mode, top right
3. Click Load unpacked and pick this folder
4. Visit an article on any of the sites listed in manifest.json
5. Read the badge in the bottom right corner

The spike picks its own target sentences, so no per-site setup exists. Click the badge
to dump a table of every target to the console. Open DevTools and switch the console
context dropdown from "top" to the ClearLens extension to see the log lines.

## Where the passages are

Three of them, one per paragraph, taken from the first three paragraphs of the
article. Green, then yellow, then red, on separate lines. A glance confirms all three.

Earlier versions scattered targets through the article and grabbed the last paragraph
to stress lazy loading. Sites replace below-fold content constantly, so the score kept
dropping for reasons unrelated to anchoring. Re-anchoring gets exercised by any DOM
change, wherever the targets sit, so the noise bought nothing.

## How a passage looks

At rest a flagged passage carries a coloured underline and nothing else, so reading
stays undisturbed. Hover over one and the fill fades in over 140ms in a lighter shade
of the same colour. Move away and the fill fades out.

Three constraints shaped this:

- ::highlight() has no :hover of its own
- highlights are paint rather than elements, so no mouse event ever fires on one
- ::highlight() ignores CSS transitions, checked against Chromium

So a mousemove handler measures the cursor against the range's own boxes, drops the
passage under it into a separate fill registry at a higher priority, and animates the
rule's alpha on a frame loop. The underline sits underneath the whole time, untouched.
A page with a strict style-src leaves the stylesheet handle null, and the fill lands
instantly instead of fading.

The wiring plan says to hit test with caretPositionFromPoint. Wrong tool. It answers
"which character is nearest the cursor", so it reports a hit from the margin, from the
line above, and from halfway across the page.

Range.getClientRects gives the geometry instead, with two catches. It returns one box
per inline fragment rather than per line, so a passage crossing a bold run hands back
three boxes sitting side by side on one line. Group them back into lines first. And a
line box is taller than the letters inside it, so trim the leading off the top of the
first line and the bottom of the last, nowhere else. Trimming every line leaves dead
strips between them and the fill drops out as the cursor crosses one.

What remains follows the text: a partial first line, full lines under it, a partial
last line, with two pixels of slack. Hovering anywhere inside that shape holds the
fill, including the gaps between the lines. The space past the end of the last line
stays outside.

The real extension opens the passage card from the same handler.

## What it refuses to run on

Section fronts. Every card on a listing page sits in its own `<article>` tag with a
summary paragraph long enough to pass for body text. Two signals give a listing away:
the paragraphs live in separate `<article>` cards, and most of them sit inside a link,
which body copy never does. Either one rejects the page.

Paywalled articles. Checked on every pass rather than once, since the wall arrives
late and a subscriber never sees one. Four signals:

- the article text shrinks to under three quarters of what loaded
- the page scroll gets locked
- the article body gets collapsed behind a fade
- a fixed panel covers more than a quarter of the viewport

The badge says which. Highlights clear, and the scan resumes by itself if the wall
goes away.

The first signal exists because of NYT, and it is the one that matters. NYT ships the
whole article, then deletes the text from the DOM about ten seconds later. No scroll
lock, no CSS clamp, no overlay across the body. Watching the character count is the
only thing that catches it.

## Reading the badge

Three squares sit above the badge, one per passage, in its own colour. Filled means
anchored. Hollow means broken. Reading three squares beats reading a fraction.

Green and 3/3 means every target anchored to the right words and drew on screen. Red
means at least one broke, and the badge names the reason. The console warns whenever
the count drops, with what triggered the re-check: scroll, resize, dom change, 10s
settle, or soft navigation.

Per target the table reports:

- crossings, how many text nodes the sentence spans. Anything above 1 crossed a link
  or a bold run, which is the hard case.
- resolved, whether the quote was found in the flattened text
- correct, whether the resulting range covers the quote and nothing else
- visible, whether the range has a non-zero box on screen
- why, one of ok, not found, wrong text, or no box

The three failures mean different things. "not found" means the text left the page, so
the sentence lived in something the site replaced. "wrong text" means an offset bug and
needs a fix here. "no box" means the range resolved but drew nothing, usually a
collapsed container or a passage inside a section the site hides at that width.

## What counts as passing

Run 10 sites from the corpus. The spike passes if 8 or more show 4/4 after all of:

- scroll to the bottom of the article
- resize the window narrow, then wide
- wait 10 seconds for lazy loading
- click through to another article without a page reload

Log the sites that fail and the reason. A site failing on extraction needs an adapter
later. Every site failing the same way means the approach needs rethinking.

## Test

test/spike_test.py runs the anchoring logic against a fixture page in headless
Chromium. The fixture covers the cases real articles break on: a sentence spanning a
link, a sentence spanning bold and italic runs, non-breaking spaces, source line breaks
inside a quote, nav and footer noise, a lazy-loaded paragraph, and a reflow.

    pip install playwright && playwright install chromium
    python3 test/spike_test.py

Every check passes as of the first commit.

## What survives

Delete the harness after the spike answers the question. Three functions move into the
real content script and are written for keeps:

- buildTextIndex, flattens the article and remembers which text node every character
  came from
- findQuote, exact match, then a whitespace-tolerant fallback, with prefix and suffix
  picking the right occurrence when a short quote repeats
- rangesFor, turns a pair of character offsets back into live DOM Ranges, one per text
  node the quote touches. Not one range across the lot: a range from the first node to
  the last also covers whatever sits between them, and the index deliberately skips
  ads, figures and captions. Sites inject ads mid-paragraph, so a single range picked
  up the ad's words and stopped matching the quote.

## One correction to the wiring plan

Section 3.2 of the plan says to extract with Readability and send the result to the
backend. Readability returns a cleaned copy of the article, so offsets into its output
point at nodes nobody rendered. Highlights need the live tree. The flat text has to
come from walking the real DOM, which is what findArticleRoot and buildTextIndex do
here. Readability still earns a place for judging whether a page holds an article and
for cleaning text the backend never has to highlight.
