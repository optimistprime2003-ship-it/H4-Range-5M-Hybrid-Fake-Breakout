// Quantum Signal Pro — service worker
// Handles incoming push notifications and taps on them.
// Runs in the background, independent of whether the app tab
// is open.

self.addEventListener('push', event => {

    let data = {};

    try {
        data = event.data ? event.data.json() : {};
    } catch (e) {
        data = {
            title: 'Quantum Signal Pro',
            body: event.data ? event.data.text() : 'New update'
        };
    }

    const title = data.title || 'Quantum Signal Pro';

    const options = {
        body: data.body || '',
        vibrate: [200, 100, 200],
        tag: 'quantum-signal',
        renotify: true
    };

    event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', event => {

    event.notification.close();

    event.waitUntil(
        clients.matchAll({ type: 'window', includeUncontrolled: true }).then(windowClients => {

            for (const client of windowClients) {
                if ('focus' in client) {
                    return client.focus();
                }
            }

            if (clients.openWindow) {
                return clients.openWindow('/');
            }
        })
    );
});
