// Management-side durable state stream.
import { SyncClient } from '../../common/SyncClient.js';
import { Api } from './api.js';
import { store } from './util.js';

export class RealtimeClient {
  constructor(onMessage, onStatus, onSnapshot = () => {}) {
    this.sync = new SyncClient({ clientType: 'admin', token: () => store.getToken(),
      snapshot: Api.snapshot, events: Api.events, onEvent: onMessage,
      onStatus, onSnapshot });
  }

  connect() { return this.sync.start(); }
  close() { this.sync.close(); }
}
