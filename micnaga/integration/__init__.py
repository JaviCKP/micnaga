"""Cableado de la app (``micnaga app``): controlador, cola, tuberia de eventos, biblioteca.

Los modulos de este paquete no importan numpy/onnxruntime/torch al importarse: la captura, las
huellas de voz y la salida del pipeline se cargan de forma perezosa.
"""
