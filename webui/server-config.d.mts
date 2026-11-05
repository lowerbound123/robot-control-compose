export type ServerConfig = {
  llm: boolean; endpoint: string; model: string; api_key: string; timeout_s: number;
  enabled: boolean; host: string; tls_name: string;
}
declare const configuration: {
  parseConfig(value: unknown): ServerConfig;
  publicConfig(config: ServerConfig): { llm: boolean; enabled: boolean; model: string; timeout_s: number };
  nginxLocation(config: ServerConfig): string;
  rcUpstream(value?: string, hosts?: string): string;
}
export default configuration
