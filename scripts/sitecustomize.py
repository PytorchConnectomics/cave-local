"""Older google-cloud-bigtable restricts emulators to loopback sockets.
Use ordinary plaintext gRPC only when the explicit emulator variable is set;
the isolated Docker network supplies the emulator endpoint.
"""
import os
if os.environ.get('BIGTABLE_EMULATOR_HOST'):
    import grpc
    from google.cloud.bigtable.client import Client
    def emulator_channel(self, transport, options):
        return grpc.insecure_channel(self._emulator_host, options=options)
    Client._emulator_channel=emulator_channel
