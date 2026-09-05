# Team presentation: Serverless on Cloudlet

Forty slides explaining the platform end to end, following the 7×7 rule
(at most seven lines per slide, at most seven words per line) except where a
slide sets `textSize` to say one or two full sentences instead. The detail lives
in the speaker notes.

| File | What it is |
|---|---|
| `serverless-platform.html` | Animated web deck. Open in any browser; arrows navigate, each line builds on a keypress, `N` toggles notes, `F` goes fullscreen. |
| `serverless-platform.pptx` | The same deck for PowerPoint, with click-to-reveal fade animations and fade transitions. |
| `serverless-platform.pdf` | Static rendering, for sharing or importing into Canva. |

Facts are taken from `docs/ARCHITECTURE.md`, `docs/BUILDING.md`, `docs/DEPLOYING.md`
and the `kpack`, `portal` and `cloudlet-apis` repositories as of September 2026.

## Regenerating

Both outputs are rendered from one content model, `generator/content.js`.

```bash
cd docs/presentation/generator
npm install
npm run build      # deck.html, deck-raw.pptx, serverless-platform.pptx
```

`gen-html.js` renders the web deck, `build2.js` renders the PowerPoint, and
`animate.js` injects the animations into the PowerPoint XML (every shape named
`step:N` fades in on the Nth click). Edit the palette and type at the top of
`gen-html.js` and `build2.js`. `logos.json` holds the project logos as inline
data URIs, so neither renderer needs the network.

Slide fields worth knowing: `reveal` picks how the visual builds (`auto`,
`paired`, `rows`, `after`), `wide` drops the text column so the visual takes the
slide, `textSize` switches the bullets to `lede` or `small`, `logo` names a key
in `logos.json`, and `clickYaml` makes each node of a graph open its `desc` and
`yaml` in the web deck.
