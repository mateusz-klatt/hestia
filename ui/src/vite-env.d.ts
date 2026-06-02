// Ambient declarations for non-TS imports that Vite resolves at build time.
// A side-effect CSS import (`import "./style.css"`) carries no value, so the
// module needs no exported members.
declare module "*.css";
