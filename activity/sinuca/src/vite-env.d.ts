/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_DASHBOARD_DECORATIVE_IMAGE_URL?: string;
  readonly VITE_DASHBOARD_DECORATIVE_IMAGE_SIZE?: string;
  readonly VITE_DASHBOARD_DECORATIVE_IMAGE_TOP?: string;
  readonly VITE_DASHBOARD_DECORATIVE_IMAGE_RIGHT?: string;
  readonly VITE_DASHBOARD_DECORATIVE_IMAGE_OPACITY?: string;
  readonly VITE_DASHBOARD_LOADING_GIF_URL?: string;
  readonly VITE_DASHBOARD_LOADING_GIF_SIZE?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
