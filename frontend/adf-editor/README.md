# CodeReviewer ADF editor island

This React/Vite island uses Atlaskit's `Editor` and `ReactRenderer` while keeping the backend API and stored ADF JSON independent from the current Web implementation. That boundary allows a later Flutter/Dart client to reuse the same ADF documents.

## Technical verification result (2026-07-15)

- `@atlaskit/editor-core@221.9.1` cannot be installed from the public registry because it references private `@atlassian/studio-entry-link`.
- The 220.x dependency graph also reaches private `@atlassian/assets-workspace-host`.
- The public-only compatibility set is pinned here to Editor Core 170 / Renderer 99 / ADF Schema 23 with React 16. It has a very large dependency graph and is therefore an optional build, not a runtime requirement for the Python service.
- If `code_reviewer/static/adf-editor.js` is present, CodeReviewer loads this island. Otherwise the built-in schema-native ADF editor/preview remains available, including Expand, tables, lists and screenshot attachments.

This result avoids making the local Web service or future Flutter client depend on inaccessible Atlassian-internal packages.

Supported editor capabilities include Expand, tables, ordered and unordered lists, panels, code blocks, rich text, and preview. Screenshots are uploaded through CodeReviewer's draft attachment API and represented as ADF `mediaSingle/media` nodes.

Build:

```powershell
npm install
npm run check
npm run build
```

Atlassian's ADF constraints are also enforced by the Python API. In particular, `expand` is top-level and may contain tables/lists/media; `nestedExpand` is only used inside a table cell or table header.
