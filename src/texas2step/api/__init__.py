from . import access, clients, cpu, measure, meta, results, wire

routers = [
    wire.router,
    meta.router,
    access.router,
    measure.router,
    results.router,
    clients.router,
    cpu.router,
]
