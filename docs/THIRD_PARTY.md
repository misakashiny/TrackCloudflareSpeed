# 第三方来源

- CloudflareSpeedTest v2.3.5：https://github.com/XIU2/CloudflareSpeedTest/tree/v2.3.5 ，GPL-3.0，原许可保存在同目录 CloudflareSpeedTest-LICENSE。通过独立子进程调用，未修改上游程序。
- Linux amd64 Release：https://github.com/XIU2/CloudflareSpeedTest/releases/tag/v2.3.5 。压缩包 SHA256：`c4c8fc76b4e1bf2bdb5ced8b765956d82dda7bc4eb59df5c04053f0f7db98d90`；二进制 SHA256：`2d0b1f831eea02ea2eb98d1cecbe9d8d631a72f19c46b23bb0457cb12f75e556`。
- 构建时自行从上游获取 CFST，仓库和源码交付不包含二进制。若再分发构建镜像，须同时履行对应版本第三方许可和源码提供义务。
- Python 官方容器与 Debian dnsmasq-base 通过 Dockerfile 引入，许可和版权资料保留在镜像的系统文档中。
