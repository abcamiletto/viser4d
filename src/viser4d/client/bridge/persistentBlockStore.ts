import type { LoadedBlock } from "./blockCache";
import { getWindow } from "./protocol";

const DB_NAME = "viser4d-runtime";
const DB_VERSION = 1;
const BLOCK_STORE = "chunk-cache-blocks";

type BlockRecord = {
  id: string;
  block: LoadedBlock;
};

export class PersistentBlockStore {
  private readonly dbPromise =
    typeof indexedDB === "undefined"
      ? Promise.resolve<IDBDatabase | null>(null)
      : openDatabase().catch((error) => {
          console.warn("[viser4d] Persistent chunk cache unavailable.", error);
          return null;
        });

  private keyPrefix = `${cacheId()}::`;

  configure(version: string): void {
    this.keyPrefix = `${cacheId()}::${version}`;
  }

  async loadBlock(blockIndex: number): Promise<LoadedBlock | null> {
    const db = await this.dbPromise;
    if (!db) {
      return null;
    }
    const tx = db.transaction(BLOCK_STORE, "readonly");
    const record = await requestAsPromise<BlockRecord | undefined>(
      tx.objectStore(BLOCK_STORE).get(this.blockKey(blockIndex)),
    );
    await transactionDone(tx);
    return record?.block ?? null;
  }

  async storeBlock(blockIndex: number, block: LoadedBlock): Promise<void> {
    const db = await this.dbPromise;
    if (!db) {
      return;
    }
    const tx = db.transaction(BLOCK_STORE, "readwrite");
    tx.objectStore(BLOCK_STORE).put({
      id: this.blockKey(blockIndex),
      block,
    } satisfies BlockRecord);
    await transactionDone(tx);
  }

  private blockKey(blockIndex: number): string {
    return `${this.keyPrefix}::${blockIndex}`;
  }
}

function cacheId(): string {
  const url = new URL(getWindow().location.href);
  url.hash = "";
  return url.toString();
}

function openDatabase(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, DB_VERSION);
    request.onerror = () => reject(request.error);
    request.onupgradeneeded = () => {
      const db = request.result;
      if (!db.objectStoreNames.contains(BLOCK_STORE)) {
        db.createObjectStore(BLOCK_STORE, { keyPath: "id" });
      }
    };
    request.onsuccess = () => resolve(request.result);
  });
}

function requestAsPromise<T>(request: IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    request.onerror = () => reject(request.error);
    request.onsuccess = () => resolve(request.result);
  });
}

function transactionDone(transaction: IDBTransaction): Promise<void> {
  return new Promise((resolve, reject) => {
    transaction.onerror = () => reject(transaction.error);
    transaction.onabort = () => reject(transaction.error);
    transaction.oncomplete = () => resolve();
  });
}
