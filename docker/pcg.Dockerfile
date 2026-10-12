FROM caveconnectome/pychunkedgraph@sha256:3794e14d0d862d33d98483dff2f6449052e6c43393117a47cb0c099ed8a26550
RUN pip install --no-cache-dir gunicorn==23.0.0 zarr==2.18.7 numcodecs==0.15.1 connected-components-3d==4.1.0 asciitree==0.3.3 deprecated==1.3.1 wrapt==2.5.0
WORKDIR /app
