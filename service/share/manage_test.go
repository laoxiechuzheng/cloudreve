package share

import (
	"database/sql"
	"database/sql/driver"
	"net/http/httptest"
	"testing"

	"github.com/DATA-DOG/go-sqlmock"
	model "github.com/cloudreve/Cloudreve/v3/models"
	"github.com/cloudreve/Cloudreve/v3/pkg/cache"
	"github.com/cloudreve/Cloudreve/v3/pkg/conf"
	"github.com/cloudreve/Cloudreve/v3/pkg/hashid"
	"github.com/cloudreve/Cloudreve/v3/pkg/serializer"
	"github.com/gin-gonic/gin"
	"github.com/jinzhu/gorm"
	"github.com/stretchr/testify/assert"
)

var mock sqlmock.Sqlmock

// TestMain 初始化数据库Mock
func TestMain(m *testing.M) {
	gin.SetMode(gin.TestMode)
	var db *sql.DB
	var err error
	db, mock, err = sqlmock.New()
	if err != nil {
		panic("An error was not expected when opening a stub database connection")
	}
	model.DB, _ = gorm.Open("mysql", db)
	defer db.Close()
	m.Run()
}

func TestShareCreateService_Create(t *testing.T) {
	a := assert.New(t)
	conf.SystemConfig.HashIDSalt = ""
	cache.Set("setting_siteURL", "https://cloud.example.com", 0)

	user := &model.User{Model: gorm.Model{ID: 1}}
	user.Group.ShareEnabled = true
	newContext := func() *gin.Context {
		c, _ := gin.CreateTestContext(httptest.NewRecorder())
		c.Set("user", user)
		return c
	}
	expectSourceFile := func() {
		mock.ExpectQuery("SELECT(.+)files(.+)").WithArgs(2, 1).
			WillReturnRows(sqlmock.NewRows([]string{"id", "name"}).AddRow(2, "deploy.sh"))
	}
	fileID := hashid.HashID(2, hashid.FileID)

	// 不设自定义链接，返回 HashID 链接
	{
		service := ShareCreateService{SourceID: fileID}
		expectSourceFile()
		mock.ExpectBegin()
		mock.ExpectExec("INSERT(.+)shares(.+)").WillReturnResult(sqlmock.NewResult(5, 1))
		mock.ExpectCommit()
		res := service.Create(newContext())
		a.Equal(0, res.Code)
		a.Equal("https://cloud.example.com/s/"+hashid.HashID(5, hashid.ShareID), res.Data)
		a.NoError(mock.ExpectationsWereMet())
	}

	// 自定义链接可用：写入并返回自定义链接
	{
		service := ShareCreateService{SourceID: fileID, CustomKey: "98k-win"}
		expectSourceFile()
		mock.ExpectQuery("SELECT(.+)shares(.+)custom_key(.+)").WithArgs("98k-win", 0).
			WillReturnRows(sqlmock.NewRows([]string{"id"}))
		mock.ExpectBegin()
		mock.ExpectExec("INSERT(.+)shares(.+)custom_key(.+)").
			WithArgs(append(anyArgs(13), "98k-win")...).
			WillReturnResult(sqlmock.NewResult(6, 1))
		mock.ExpectCommit()
		res := service.Create(newContext())
		a.Equal(0, res.Code)
		a.Equal("https://cloud.example.com/s/98k-win", res.Data)
		a.NoError(mock.ExpectationsWereMet())
	}

	// 自定义链接已被其他分享使用：不写入
	{
		service := ShareCreateService{SourceID: fileID, CustomKey: "98k-win"}
		expectSourceFile()
		mock.ExpectQuery("SELECT(.+)shares(.+)custom_key(.+)").WithArgs("98k-win", 0).
			WillReturnRows(sqlmock.NewRows([]string{"id", "source_name"}).AddRow(7, "old.exe"))
		res := service.Create(newContext())
		a.Equal(serializer.CodeLinkKeyTaken, res.Code)
		a.Equal("old.exe", res.Msg)
		a.NoError(mock.ExpectationsWereMet())
	}

	// 自定义链接格式不对：不查询、不写入
	{
		service := ShareCreateService{SourceID: fileID, CustomKey: "a/b"}
		expectSourceFile()
		res := service.Create(newContext())
		a.Equal(serializer.CodeLinkKeyInvalid, res.Code)
		a.NoError(mock.ExpectationsWereMet())
	}
}

// anyArgs 返回 n 个任意值参数匹配
func anyArgs(n int) []driver.Value {
	args := make([]driver.Value, n)
	for i := range args {
		args[i] = sqlmock.AnyArg()
	}
	return args
}
