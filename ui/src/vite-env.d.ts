// Ambient declarations for non-TS imports that Vite resolves at build time.
// A side-effect CSS import (`import "./style.css"`) carries no value, so the
// module needs no exported members.
declare module "*.css";

// Minimal typing for the Vite build-time env (only what we use): `import.meta.env.DEV` is `true` in
// `npm run dev` and statically `false` in `vite build`, so a `if (import.meta.env.DEV)` branch (the
// dev-only API mock) is dead-eliminated from the production bundle.
interface ImportMetaEnv {
  readonly DEV: boolean;
}
interface ImportMeta {
  readonly env: ImportMetaEnv;
}
