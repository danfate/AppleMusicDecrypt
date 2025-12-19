FROM python:3-alpine AS builder

WORKDIR /build

# 1. 设置 Alpine 镜像源（可选，如果你在中国大陆，这能极大加速 apk 和 git）
# RUN sed -i 's/dl-cdn.alpinelinux.org/mirrors.tuna.tsinghua.edu.cn/g' /etc/apk/repositories

# 2. 集中安装编译依赖 (合并 Layer)
RUN apk add --no-cache git g++ make cmake zlib-dev zlib-static coreutils pkgconf

# 3. 编译 GPAC
RUN set -eux; \
    git clone --depth=1 https://github.com/gpac/gpac.git ./gpac; \
    cd ./gpac; \
    ./configure --static-bin --prefix=/usr/local; \
    make -j$(nproc); \
    make install

# 4. 编译 Bento4 (利用缓存)
RUN set -eux; \
    git clone --depth=1 https://github.com/axiomatic-systems/Bento4.git ./Bento4; \
    mkdir -p ./Bento4/cmakebuild; \
    cd ./Bento4/cmakebuild; \
    cmake -DCMAKE_BUILD_TYPE=Release ..; \
    make -j$(nproc); \
    make install

# --- Final Stage ---
FROM python:3-alpine

WORKDIR /app


# 5. 安装运行时所需的系统库 (ffmpeg, curl) 和 pip 安装 Poetry
# 使用 pip 安装 poetry 通常比 curl 脚本更快且更容易利用 pip 缓存
RUN set -eux; \
    apk add --no-cache ffmpeg libstdc++ libgcc; \
    pip install --no-cache-dir poetry

# 6. 从 Builder 阶段复制编译好的二进制文件
# GPAC 通常安装在 /usr/local/bin 和 /usr/local/lib
COPY --from=builder /usr/local/bin/* /usr/local/bin/
COPY --from=builder /usr/local/lib/* /usr/local/lib/


RUN ln -sf /usr/local/bin/MP4Box /usr/local/bin/mp4box &&  MP4Box -version

# 7. 【关键优化】先只复制依赖描述文件
COPY pyproject.toml ./

# 8. 安装 Python 依赖 (如果 pyproject.toml 没变，这一步会直接使用缓存)
RUN poetry config virtualenvs.create false && \
    poetry install --no-root --no-interaction --no-ansi

# 9. 【关键优化】最后才复制源代码
# 这样修改代码时，只会重新执行这一步，耗时 < 1s
COPY . .

CMD ["sh", "-c", "umask 000 && poetry run python main.py"]