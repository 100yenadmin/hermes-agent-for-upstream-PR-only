export interface PluginSettingsFormCopy {
  save: string
  saved: (name: string) => string
  saveFailed: (name: string) => string
  required: string
  secretSet: string
  secretEmpty: string
  secretStoredAs: (env: string) => string
}
