from app import create_app, db

app = create_app()

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    # 启动调度器
    from app.scheduler import MonitorScheduler
    scheduler = MonitorScheduler(app)
    scheduler.init_app(app)
    app.scheduler = scheduler   # 将调度器附加到 app 实例，供 API 使用
    app.run(host='0.0.0.0', debug=False)
