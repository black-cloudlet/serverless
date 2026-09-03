# Team presentation: Serverless on Cloudlet

`serverless-platform.pptx` is a 23-slide deck explaining the platform end to end:
what serverless and Knative are, why the OpenShift Serverless Operator was chosen,
the API and its structure, mTLS to the clusters, the active/active topology, the
tenant controller, why functions differ from containers, buildpacks and kpack, the
build controller, the portal, and how `cloudlet-apis` shortens the next API.

Every content slide carries speaker notes. Facts on the slides are taken from
`docs/ARCHITECTURE.md`, `docs/BUILDING.md`, `docs/DEPLOYING.md`, and the
`kpack`, `portal` and `cloudlet-apis` repositories as of September 2026.

## Regenerating

The deck is produced by a `pptxgenjs` script in `generator/`:

```bash
cd docs/presentation/generator
npm install
npm run build          # writes serverless-platform.pptx next to build.js
```

Edit slide content in `build.js`; palette, fonts and layout helpers live at the
top of that file. Move the resulting `.pptx` up one directory to replace the
committed copy.
