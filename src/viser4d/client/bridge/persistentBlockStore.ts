import type { LoadedBlock } from "./blockCache";
import { getWindow } from "./protocol";

const DB_NAME = "viser4d-runtime";
const DB_VERSION = 1;
const META_STORE = "chunk-cache-meta";
const BLOCK_STORE = "chunk-cache-blocks";

type MetaRecord = {
  cacheId: string;
  version: string;
};

type BlockRecord = {
  id: string;
  checkpointMessages: LoadedBlock["checkpointMessages"];
  stepMessages: LoadedBlock["stepMessages"];
};

export class PersistentBlockStore {
  private activeCacheId: string | null = null;
  private activeVersion: string | null = null;
  private databasePromise: Promise<IDBDatabase | null> | null = null;
  private disabled = typeof indexedDB === "undefined";
  private ready: Promise<void> = Promise.resolve();
  private session = 0;

  configure(version: string): void {
    this.session += 1;
    this.activeCacheId = getCacheId();
    this.activeVersion = version;
    const session = this.session;
    this.ready = this.prepare(session);
  }

  async loadBlock(blockIndex: number): Promise<LoadedBlock | null> {
    const session = this.session;
    const cacheId = this.activeCacheId;
    if (this.disabled || cacheId === null) {
      return null;
    }
    try {
      await this.ready;
      if (session !== this.session || cacheId !== this.activeCacheId) {
        return null;
      }
      const db = await this.getDatabase();
      if (!db) {
        return null;
      }
      const tx = db.transaction(BLOCK_STORE, "readonly");
      const record = await requestAsPromise<BlockRecord | undefined>(
        tx.objectStore(BLOCK_STORE).get(blockKey(cacheId, blockIndex)),
      );
      await transactionDone(tx);
      if (session !== this.session) {
        return null;
      }
      return record
        ? {
            checkpointMessages: record.checkpointMessages,
            stepMessages: record.stepMessages,
          }
        : null;
    } catch (error) {
      this.disable(error);
      return null;
    }
  }

  async storeBlock(blockIndex: number, block: LoadedBlock): Promise<void> {
    const session = this.session;
    const cacheId = this.activeCacheId;
    if (this.disabled || cacheId === null) {
      return;
    }
    try {
      await this.ready;
      if (session !== this.session || cacheId !== this.activeCacheId) {
        return;
      }
      const db = await this.getDatabase();
      if (!db) {
        return;
      }
      const tx = db.transaction(BLOCK_STORE, "readwrite");
      tx.objectStore(BLOCK_STORE).put({
        id: blockKey(cacheId, blockIndex),
        checkpointMessages: block.checkpointMessages,
        stepMessages: block.stepMessages,
      } satisfies BlockRecord);
      await transactionDone(tx);
    } catch (error) {
      this.disable(error);
    }
  }

  private async prepare(session: number): Promise<void> {
    const cacheId = this.activeCacheId;
    const version = this.activeVersion;
    if (this.disabled || cacheId === null || version === null) {
      return;
    }
    try {
      const db = await this.getDatabase();
      if (!db || session !== this.session) {
        return;
      }
      const metaTx = db.transaction(META_STORE, "readonly");
      const existingMeta = await requestAsPromise<MetaRecord | undefined>(
        metaTx.objectStore(META_STORE).get(cacheId),
      );
      await transactionDone(metaTx);
      if (session !== this.session || existingMeta?.version === version) {
        return;
      }
      await this.clearCache(db, cacheId);
      if (session !== this.session) {
        return;
      }
      const writeTx = db.transaction(META_STORE, "readwrite");
      writeTx.objectStore(META_STORE).put({ cacheId, version } satisfies MetaRecord);
      await transactionDone(writeTx);
    } catch (error) {
      this.disable(error);
    }
  }

  private async clearCache(db: IDBDatabase, cacheId: string): Promise<void> {
    const blockTx = db.transaction(BLOCK_STORE, "readwrite");
    await deleteKeyRange(blockTx.objectStore(BLOCK_STORE), blockKeyRange(cacheId));
    await transactionDone(blockTx);

    const metaTx = db.transaction(META_STORE, "readwrite");
    metaTx.objectStore(META_STORE).delete(cacheId);
    await transactionDone(metaTx);
  }

  private async getDatabase(): Promise<IDBDatabase | null> {
    if (this.disabled) {
      return null;
    }
    if (this.databasePromise === null) {
      this.databasePromise = openDatabase().catch((error) => {
        this.disable(error);
        return null;
      });
    }
    return this.databasePromise;
  }

  private disable(error: unknown): void {
    this.disabled = true;
    this.databasePromise = null;
    console.warn("[viser4d] Persistent chunk cache disabled.", error);
  }
}

function getCacheId(): string {
  const url = new URL(getWindow().location.href);
  url.hash = "";
  return url.toString();
}

function blockKey(cacheId: string, blockIndex: number): string {
  return `${cacheId}::${blockIndex}`;
}

function blockKeyRange(cacheId: string): IDBKeyRange {
  const prefix = `${cacheId}::`;
  return IDBKeyRange.bound(prefix, `${prefix}\uffff`);
}

function openDatabase(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, DB_VERSION);
    request.onerror = () => reject(request.error);
    request.onupgradeneeded = () => {
      const db = request.result;
      if (!db.objectStoreNames.contains(META_STORE)) {
        db.createObjectStore(META_STORE, { keyPath: "cacheId" });
      }
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

function deleteKeyRange(
  store: IDBObjectStore,
  range: IDBKeyRange,
): Promise<void> {
  return new Promise((resolve, reject) => {
    const request = store.openKeyCursor(range);
    request.onerror = () => reject(request.error);
    request.onsuccess = () => {
      const cursor = request.result;
      if (!cursor) {
        resolve();
        return;
      }
      store.delete(cursor.primaryKey);
      cursor.continue();
    };
  });
}
