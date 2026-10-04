// Minimal ambient types so the LSP resolves KVNamespace without npm-installing
// @cloudflare/workers-types. wrangler deploy/dev use their own bundled types
// at build time regardless of what's in node_modules.
declare interface KVNamespaceGetOptions<T> {
  type?: T;
  cacheTtl?: number;
}

declare interface KVNamespace {
  get(key: string, options?: "text" | KVNamespaceGetOptions<"text">): Promise<string | null>;
  get(key: string, options: "json" | KVNamespaceGetOptions<"json">): Promise<unknown>;
  get(key: string, options: "arrayBuffer" | KVNamespaceGetOptions<"arrayBuffer">): Promise<ArrayBuffer | null>;
  get(key: string, options: "stream" | KVNamespaceGetOptions<"stream">): Promise<ReadableStream | null>;
  put(
    key: string,
    value: string | ArrayBuffer | ArrayBufferView | ReadableStream,
    options?: { expiration?: number; expirationTtl?: number; metadata?: unknown }
  ): Promise<void>;
  delete(key: string): Promise<void>;
  list(options?: { prefix?: string; limit?: number; cursor?: string }): Promise<{
    keys: Array<{ name: string }>;
    list_complete: boolean;
    cursor?: string;
  }>;
}

declare interface ExecutionContext {
  waitUntil(promise: Promise<unknown>): void;
  passThroughOnException(): void;
}
