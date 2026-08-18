# 数据库迁移

迁移由 Alembic 管理，生产部署只自动执行向前迁移：

```shell
python -m alembic upgrade head
```

数据库地址从 `OPENREVIEWER_DATABASE_URL` 读取；容器部署也可以使用
`OPENREVIEWER_DB_HOST`、`OPENREVIEWER_DB_PORT`、`OPENREVIEWER_DB_NAME`、
`OPENREVIEWER_DB_USER` 和 `OPENREVIEWER_DB_PASSWORD` 分项配置。

已经发布的迁移不得修改、重命名或删除。部署流程不自动执行降级。
