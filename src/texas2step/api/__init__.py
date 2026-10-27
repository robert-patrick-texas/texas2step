from . import access, clients, measure, meta, results, wire

routers = [
    wire.router,
    meta.router,
    access.router,
    measure.router,
    results.router,
    clients.router,
]
